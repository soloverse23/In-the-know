"""View model: language-neutral blocks for the in-chat UI, and their text form for the model.

One source of truth: tools build blocks; the UI renders them (translated, branded) and the
model gets the same content as compact English Markdown plus short hints.
"""
from __future__ import annotations

from typing import Any, Optional

from . import i18n

# --- builders -------------------------------------------------------------------


def T(key: str, **p) -> dict:
    return {"t": key, "p": p} if p else {"t": key}


def M(minor) -> dict:
    return {"$m": minor}


def N(x) -> dict:
    return {"$n": x}


def P(x) -> dict:
    return {"$p": x}


def D(iso) -> dict:
    return {"$d": iso}


def MO(ym) -> dict:
    return {"$mo": ym}


def kpi(key: str, value, prev=None, fmt: str = "money", good: str = "up", delta=None) -> dict:
    return {"key": key, "label": f"kpi.{key}", "value": value, "prev": prev, "fmt": fmt, "good": good, "delta": delta}


def col(key: str, fmt: str = "text", label: Optional[str] = None) -> dict:
    return {"k": key, "t": label or f"col.{key}", "fmt": fmt}


def view(name: str, title: dict, blocks: list[dict], subtitle: Optional[dict] = None, hints: Optional[list[str]] = None,
         summary: Optional[str] = None) -> dict:
    return {"view": name, "title": title, "subtitle": subtitle, "blocks": [b for b in blocks if b], "hints": hints or [],
            "summary": summary}


# --- text rendering for the model ------------------------------------------------


def _txt(x, cur: str) -> str:
    if x is None:
        return ""
    if isinstance(x, dict) and "raw" in x and "t" not in x:
        return str(x["raw"])
    if isinstance(x, dict) and "t" in x:
        return i18n.t(x["t"], "en", x.get("p"), cur)
    if isinstance(x, dict):
        return i18n.fmt_value(x, "en", cur)
    return str(x)


def _cell(v, fmt: str, cur: str) -> str:
    if v is None or v == "":
        return "—"
    if fmt == "money":
        return i18n.money(v, cur, "en")
    if fmt == "percent":
        return f"{v:.1f}%"
    if fmt == "date":
        return str(v)[:10]
    if fmt == "days":
        return f"{v}d"
    if fmt == "number":
        return i18n.num(v, "en", 0 if float(v).is_integer() else 1)
    if isinstance(v, dict):
        return _txt(v, cur)
    return str(v)


def to_text(v: dict, currency: str = "EUR", max_rows: int = 15) -> str:
    out = [f"# {_txt(v['title'], currency)}"]
    if v.get("subtitle"):
        out.append(_txt(v["subtitle"], currency))
    if v.get("summary"):
        out.append(v["summary"])
    for b in v["blocks"]:
        out.append("")
        t = b["type"]
        if b.get("title"):
            out.append(f"## {_txt(b['title'], currency)}")
        if t == "kpis":
            for k in b["items"]:
                delta = "" if k.get("delta") is None else (f" ({k['delta']:+.1f} pt)" if k["fmt"] == "percent" else f" ({k['delta']:+.1f}%)")
                prev = "" if k.get("prev") is None else f", previous {_cell(k['prev'], k['fmt'], currency)}"
                out.append(f"- {_txt({'t': k['label']}, currency)}: {_cell(k['value'], k['fmt'], currency)}{prev}{delta}")
        elif t == "alerts":
            out += [f"- {'⚠ ' if a.get('level') == 'warn' else ''}{_txt(a, currency)}" for a in b["items"]] or ["- none"]
        elif t == "table":
            cols = b["columns"]
            out.append("| " + " | ".join(c.get("raw") or _txt({"t": c["t"]}, currency) for c in cols) + " |")
            out.append("|" + "---|" * len(cols))
            for r in b["rows"][:max_rows]:
                out.append("| " + " | ".join(_cell(r.get(c["k"]), c["fmt"], currency) for c in cols) + " |")
            if len(b["rows"]) > max_rows or b.get("more"):
                out.append(f"(+{(b.get('more') or 0) + max(0, len(b['rows']) - max_rows)} more)")
        elif t == "entity":
            out.append(f"**{b['name']}**" + (f" · {b['subtitle']}" if b.get("subtitle") else "") + f" · status: {b.get('status')}")
            out += [f"- {_txt({'t': f['t']}, currency)}: {_cell(f['v'], f['fmt'], currency)}" for f in b["facts"]]
            if b.get("notes"):
                out.append("Notes: " + " / ".join(b["notes"]))
            if b.get("extra"):
                out.append("Other: " + ", ".join(f"{k}: {v}" for k, v in b["extra"].items()))
            out += [f"- {_txt(w, currency)}" for w in b.get("warnings", [])]
        elif t == "timeline":
            out += [f"- {i['date']}: {i.get('title') or ''}{' · ' + i['sub'] if i.get('sub') else ''} · {_cell(i.get('amount'), 'money', currency)}"
                    + (f" [{i['status']}]" if i.get("status") and i["status"] != "done" else "") for i in b["items"][:max_rows]]
        elif t == "chart":
            out.append(", ".join(f"{s['label']}: {_cell(s['value'], b.get('fmt', 'money'), currency)}" for s in b["series"]))
        elif t == "feed":
            icon = {"saved": "SAVED", "known": "ALREADY KNOWN", "clarify": "NEEDS CLARIFICATION", "error": "ERROR", "undone": "UNDONE"}
            for i in b["items"]:
                chips = " · ".join(_cell(c["v"], "text", currency) if not isinstance(c["v"], dict) else _txt(c["v"], currency) for c in i.get("chips") or [])
                note = f" ({_txt(i['note'], currency)})" if i.get("note") else ""
                undo = f" [undo id: {i['undo']}]" if i.get("undo") else ""
                out.append(f"- [{icon.get(i['status'], i['status'])}] {_txt(i, currency)}{': ' + chips if chips else ''}{note}{undo}")
        elif t == "files":
            for f in b["items"]:
                line = f"- {f['name']} → {f.get('entity') or 'not recognized'}"
                if f.get("already"):
                    line += " (already imported)"
                elif f.get("entity") not in (None, "unknown", "skipped", "knowledge"):
                    line += f": {f['loaded']} new, {f.get('updated', 0)} updated, {f.get('duplicate', 0)} duplicates skipped, {f.get('flagged', 0)} rejected"
                    if f.get("date_range"):
                        line += f", dates {f['date_range'][0]} → {f['date_range'][1]}"
                elif f.get("entity") == "knowledge":
                    line += f": {f['loaded']} note sections"
                out.append(line)
                if f.get("mapping"):
                    out.append("  columns: " + "; ".join(f"{m['h']} → {m['target']}" for m in f["mapping"]))
                for nf in f.get("new_fields", []):
                    out.append(f"  new field '{nf['field']}' ({nf['type']}) from column '{nf['header']}' [undo id {nf['change_id']}]")
                if f.get("notes"):
                    out.append("  " + "; ".join(f["notes"][:4]))
                if f.get("headers"):
                    out.append(f"  headers: {f['headers']}; sample: {f.get('sample')}")
        elif t == "checklist":
            mark = {"done": "x", "current": ">", "todo": " "}
            out += [f"- [{mark[i['state']]}] {_txt({'t': i['t']}, currency)}" for i in b["items"]]
        elif t == "schema":
            for e in b["entities"]:
                out.append(f"- {e['name']} ({e['role']}): " + ", ".join(f"{f['name']}:{f['type']}" for f in e["fields"]))
            for c in b.get("changes", []):
                out.append(f"  change #{c['id']}: {c['op']} {c.get('entity')}.{c.get('field') or ''} ({c['status']})")
        elif t == "questions":
            out += [f"- [{q['id']}] {q['text']}" for q in b["items"]] or ["- none"]
        elif t == "copy":
            out.append("```\n" + b["text"] + "\n```")
            out += [f"{n}. {_txt({'t': k}, currency)}" for n, k in enumerate(b.get("steps") or [], 1)]
        elif t == "kv":
            out += [f"- {i['raw'] if 'raw' in i else _txt({'t': i['t']}, currency)}: {_cell(i['v'], i.get('fmt', 'text'), currency)}" for i in b["items"]]
        elif t in ("hero",):
            out.append(_txt(b, currency))
        elif t == "tips":
            out += [f"- {_txt(i, currency)}" for i in b["items"]]
        elif t == "progress":
            out.append(f"progress: {round(b['value'] * 100)}%")
        elif t == "text":
            out.append(_txt(b, currency))
        elif t == "notes":
            out += [f"- {n['title']}: {n['text']}" for n in b["items"]]
        elif t == "facts":
            mark = {"likes": "+", "dislikes": "−", "must": "MUST", "must_not": "NEVER", "neutral": "·"}
            conf = {"stated": " ?", "inferred": " ~"}
            for f in b["items"]:
                prev = f" (was “{f['previous']['value']}” on {f['previous']['date']})" if f.get("previous") else ""
                src = ", ".join(x for x in (f.get("date"), f.get("source")) if x)
                quote = f"; “{f['quote']}”" if f.get("quote") and f["quote"] != f["value"] else ""
                out.append(f"- [fact:{f['id']}] {mark.get(f['polarity'], '·')} {f['label']['en']}{' (' + f['key'] + ')' if f.get('key') else ''}: "
                           f"{f['value']}{conf.get(f['confidence'], '')}{prev} [{src}{quote}]")
            for m in b.get("masked") or []:
                out.append(f"- {m['label']['en']}: {m['n']} sensitive fact(s) hidden")
            if b.get("missing"):
                out.append("Not known yet: " + ", ".join(m["label"]["en"] for m in b["missing"]))
        elif t == "relations":
            for r in b["items"]:
                about = "; ".join(f"{'+' if f['polarity'] == 'likes' else '−' if f['polarity'] == 'dislikes' else f['polarity'].upper()} {f['value']}"
                                  for f in r.get("facts") or [])
                out.append(f"- {r['name']}: {r['label']['en']} {b.get('person', '')}".rstrip() + (f" ({r['note']})" if r.get("note") else "")
                           + (f" · about {r['name']}: {about}" if about else ""))
        elif t == "design":
            out.append(f"brand: {b['brand']}")
            out.append(f"brief sections: {b['brief']['sections']}; brief kpis: {b['brief']['kpis']}")
            if b.get("views"):
                out.append(f"custom views: {list(b['views'])}")
    if v.get("hints"):
        out.append("")
        out += [f"> {h}" for h in v["hints"]]
    return "\n".join(out)


def block(type_: str, **kw: Any) -> dict:
    return {"type": type_, **kw}


def schema_field_view(f: dict) -> dict:
    """Field description for the schema card (translated by the UI via attr.* keys or the field's own labels)."""
    out = {"name": f["name"], "type": f["type"]}
    if f.get("role_attr") and f["type"] != "ref":
        out["tk"] = f"attr.{f['role_attr']}"
    elif f["type"] == "ref":
        out["ref"] = f.get("ref")
    lab = f.get("label") or {}
    if lab:
        out["label"] = {lg: (v[0] if isinstance(v, list) else v) for lg, v in lab.items()}
    return out
