"""Compare answers with raw files in context ("baseline", like a Claude Project) against
answers through the context engine's tools ("engine", like our MCP connector).

Usage:
  uv run python eval/run_eval.py --dry-run                     # sizes only, no API calls
  uv run python eval/run_eval.py --models claude-haiku-5-5 --questions q01_revenue
  uv run python eval/run_eval.py                               # full run: 3 models x 2 conditions x 12 questions
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import sys
import time
from pathlib import Path

import anthropic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))
from questions import BUSINESSES, DEMO, truth  # noqa: E402

from intheknow.context import SERVER_INSTRUCTIONS, ContextEngine  # noqa: E402
from intheknow.ingest import read_table  # noqa: E402
from intheknow.mcp_server import build_server  # noqa: E402

TODAY = dt.date(2026, 11, 1)
JUDGE_MODEL = "claude-opus-5-5"
DEFAULT_MODELS = ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"]
# USD per 1M tokens: input, output, cache read. Cache writes billed at 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
LANG_NAME = {"ro": "Romanian", "fr": "French", "de": "German", "en": "English"}


def role(biz: str) -> str:
    s = BUSINESSES[biz]["setup"]
    return (f"You are the business advisor of the owner of «{s['name']}» ({s['vertical']}). Today is {TODAY.isoformat()}. "
            f"Answer in {LANG_NAME[s['locale']]}, concisely, with specific numbers. Currency: {s['currency']}.")


def baseline_context(biz: str) -> str:
    parts = []
    for path in sorted((DEMO / biz).iterdir()):
        text = read_table(path)[2] if path.suffix in (".csv", ".xlsx") else path.read_text(encoding="utf-8")
        parts.append(f"<file name=\"{path.name}\">\n{text}\n</file>")
    return "Files the owner uploaded to this project:\n\n" + "\n\n".join(parts)


def build_db(biz: str, path: Path) -> None:
    e = ContextEngine(path, TODAY)
    e.call_view("setup_business", BUSINESSES[biz]["setup"])
    e.call_view("add_data", {"paths": [str(DEMO / biz)]})


def tool_schemas() -> list[dict]:
    import asyncio
    import tempfile
    srv = build_server(ContextEngine(Path(tempfile.mkdtemp()) / "s.db", TODAY))
    return [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in asyncio.run(srv.list_tools())]


def usage_cost(model: str, u: dict) -> float:
    pin, pout, pcache = PRICES[model]
    return (u["input"] * pin + u["cache_write"] * pin * 1.25 + u["cache_read"] * pcache + u["output"] * pout) / 1e6


def _add_usage(total: dict, usage) -> None:
    total["input"] += usage.input_tokens
    total["output"] += usage.output_tokens
    total["cache_read"] += usage.cache_read_input_tokens or 0
    total["cache_write"] += usage.cache_creation_input_tokens or 0


def answer(client: anthropic.Anthropic, model: str, condition: str, question: dict, db_path: Path, files_text: str, biz: str, tools_all: list) -> dict:
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    tool_calls: list[dict] = []
    messages = [{"role": "user", "content": question["text"]}]
    if condition == "baseline":
        system = [{"type": "text", "text": role(biz)}, {"type": "text", "text": files_text, "cache_control": {"type": "ephemeral"}}]
        tools = []
    else:
        system = [{"type": "text", "text": role(biz) + "\n\n" + SERVER_INSTRUCTIONS, "cache_control": {"type": "ephemeral"}}]
        tools = tools_all
        engine = ContextEngine(db_path, TODAY)
    started = time.time()
    stop = None
    for _ in range(15):
        kwargs = dict(model=model, max_tokens=16000, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        response = client.messages.create(**kwargs)
        _add_usage(usage, response.usage)
        stop = response.stop_reason
        messages.append({"role": "assistant", "content": response.content})
        if stop != "tool_use":
            break
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                out = engine.call(block.name, dict(block.input))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
            except Exception as exc:  # noqa: BLE001 - surface tool errors to the model
                out = f"Error: {exc}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out, "is_error": True})
            tool_calls.append({"name": block.name, "input": dict(block.input), "result_chars": len(out)})
        messages.append({"role": "user", "content": results})
    text = "\n".join(b.text for b in messages[-1]["content"] if getattr(b, "type", None) == "text") if messages[-1]["role"] == "assistant" else ""
    return {
        "model": model, "condition": condition, "qid": question["id"], "business": biz, "kind": question["kind"],
        "answer": text, "stop_reason": stop, "tool_calls": tool_calls, "usage": usage,
        "cost_usd": round(usage_cost(model, usage), 5), "seconds": round(time.time() - started, 1),
    }


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"index": {"type": "integer"}, "passed": {"type": "boolean"}, "note": {"type": "string"}},
                "required": ["index", "passed", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["criteria"],
    "additionalProperties": False,
}


def judge(client: anthropic.Anthropic, question: dict, ans: str, biz: str) -> dict:
    criteria = "\n".join(f"{i}. {c}" for i, c in enumerate(question["criteria"]))
    prompt = (
        "You grade an AI business advisor's answer for a small business. Ground truth (computed from the clean data; money in minor units):\n"
        f"{json.dumps(truth(biz), ensure_ascii=False)}\n\n"
        f"Question: {question['text']}\n\nAnswer to grade:\n<answer>\n{ans or '(empty)'}\n</answer>\n\n"
        f"Criteria:\n{criteria}\n\n"
        "For each criterion decide passed true/false. Be strict about numbers and tolerances; a number that is not "
        "stated does not pass. Equivalent formats (e.g. 17.110 lei, 17,1 mii lei) count. Return one entry per criterion index."
    )
    response = client.messages.create(
        model=JUDGE_MODEL, max_tokens=4000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    items = json.loads(text)["criteria"]
    passed = sum(1 for c in items if c["passed"])
    return {"score": passed / len(question["criteria"]), "criteria": items, "judge_cost_usd": round(usage_cost(JUDGE_MODEL, {
        "input": response.usage.input_tokens, "output": response.usage.output_tokens, "cache_read": 0, "cache_write": 0}), 5)}


def summarize(rows: list[dict], models: list[str]) -> str:
    lines = ["# Eval: raw files in context vs. In The Know engine", "",
             f"Run: {dt.datetime.now().isoformat(timespec='minutes')} | judge: {JUDGE_MODEL} | scenario date: {TODAY}", ""]
    lines.append("| Model | Condition | Score (all) | Facts fully correct | Advice score | Avg tool calls | Cost (USD) | Avg seconds |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for m in models:
        for cond in ("baseline", "engine"):
            rs = [r for r in rows if r["model"] == m and r["condition"] == cond and "score" in r]
            if not rs:
                continue
            facts = [r for r in rs if r["kind"] == "fact"]
            adv = [r for r in rs if r["kind"] == "advice"]
            lines.append(
                f"| {m} | {cond} | {sum(r['score'] for r in rs) / len(rs):.0%} | "
                f"{sum(r['score'] == 1 for r in facts)}/{len(facts)} | "
                f"{(sum(r['score'] for r in adv) / len(adv)) if adv else 0:.0%} | "
                f"{sum(len(r['tool_calls']) for r in rs) / len(rs):.1f} | "
                f"{sum(r['cost_usd'] for r in rs):.2f} | {sum(r['seconds'] for r in rs) / len(rs):.0f} |"
            )
    lines += ["", "## Per question (score baseline → engine)", "", "| Question | " + " | ".join(models) + " |", "|---|" + "---|" * len(models)]
    for q in [q for b in BUSINESSES.values() for q in b["questions"]()]:
        cells = []
        for m in models:
            b = next((r for r in rows if r["model"] == m and r["condition"] == "baseline" and r["qid"] == q["id"] and "score" in r), None)
            e = next((r for r in rows if r["model"] == m and r["condition"] == "engine" and r["qid"] == q["id"] and "score" in r), None)
            cells.append(f"{b['score']:.0%} → {e['score']:.0%}" if b and e else "—")
        lines.append(f"| {q['id']} | " + " | ".join(cells) + " |")
    judge_cost = sum(r.get("judge_cost_usd", 0) for r in rows)
    lines += ["", f"Total answer cost: ${sum(r['cost_usd'] for r in rows):.2f} | judge cost: ${judge_cost:.2f}"]
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--models", nargs="+", default=DEFAULT_MODELS)
    p.add_argument("--conditions", nargs="+", default=["baseline", "engine"])
    p.add_argument("--businesses", nargs="+", default=list(BUSINESSES))
    p.add_argument("--questions", nargs="+", default=None)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    out_dir = ROOT / "eval" / "results" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    tools_all = tool_schemas()
    setups = {}
    for biz in args.businesses:
        db = out_dir / f"{biz}.db"
        build_db(biz, db)
        qs = [q for q in BUSINESSES[biz]["questions"]() if not args.questions or q["id"] in args.questions]
        setups[biz] = (db, baseline_context(biz), qs)
        if args.dry_run:
            brief = ContextEngine(db, TODAY).call("get_business_brief", {})
            print(f"{biz}: baseline context {len(setups[biz][1]):,} chars | engine brief {len(brief):,} chars | {len(qs)} questions")
    if args.dry_run:
        n = sum(len(v[2]) for v in setups.values()) * len(args.models) * len(args.conditions)
        print(f"runs: {n} (+ same number of judge calls); tools: {len(tools_all)}")
        return

    client = anthropic.Anthropic()
    jobs = [(m, c, biz, q) for biz, (_, _, qs) in setups.items() for q in qs for m in args.models for c in args.conditions]
    rows: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(answer, client, m, c, q, setups[biz][0], setups[biz][1], biz, tools_all): (m, c, biz, q) for m, c, biz, q in jobs}
        for fut in cf.as_completed(futures):
            m, c, biz, q = futures[fut]
            try:
                row = fut.result()
                row.update(judge(client, q, row["answer"], biz))
            except anthropic.APIError as exc:
                row = {"model": m, "condition": c, "qid": q["id"], "kind": q["kind"], "business": biz, "error": str(exc),
                       "tool_calls": [], "cost_usd": 0, "seconds": 0}
            rows.append(row)
            print(f"{m:20s} {c:8s} {q['id']:16s} score={row.get('score', 'ERR')}")
            with open(out_dir / "results.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = summarize(rows, args.models)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary + f"\n\nResults: {out_dir}")


if __name__ == "__main__":
    main()
