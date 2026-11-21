"""Context engine: intent-based read tools that return a view model (blocks) + English text.

Numbers are always computed here (metrics.py), never by the model. Every view is
language-neutral; the UI renders it in the owner's language and brand.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

from . import design, i18n, schema
from . import metrics as M
from .db import get_setting, load_into, open_memory, readonly_copy
from .store import Conflict, LocalStore, Store
from .normalize import fold, name_key, phone_key
from .editing import EditOps
from .onboarding import OnboardingOps
from .operations import WriteOps
from .people import PeopleOps
from .views import schema_field_view, D, MO, N, P, T, block, col, kpi, to_text, view

KPI_FMT = {"revenue": ("money", "up"), "transactions": ("number", "up"), "receipts": ("number", "up"),
           "unique_customers": ("number", "up"), "new_customers": ("number", "up"), "avg_ticket": ("money", "up"),
           "cost_of_goods": ("money", "down"), "gross_profit": ("money", "up"), "expenses": ("money", "down"),
           "net_profit": ("money", "up"), "no_show_rate": ("percent", "down"), "items_sold": ("number", "up")}

QUERY_MAX_ROWS = 25             # query_data text cap: the model should aggregate, not read tables

SERVER_INSTRUCTIONS = """\
In The Know is the memory and analyst of ONE small business (the user is its owner).
Reply in the owner's language. Business numbers come only from these tools.

CONTEXT, quietly: at the start of a conversation call get_context (no card). Do not open with cards.

CARDS only when they answer the message. Several tools draw a card in the chat. Call one only when the owner asked to SEE that
thing: get_business_brief (overview / "how is business"), get_action_list (who to call / reminders / this week),
review_period (a period review), get_person_brief (one person), find_customers (a list), get_data_status, browse_records,
show_view. For a quick question ("how much did we make in October?") use get_metrics or query_data and answer in text.

ANSWERING from the data: let the server do the reading. Never pull a whole table to look through it.
- Ask for exactly what the question needs: find_records with a filter (where / search), only the fields you need, a sort and a
  small limit, or count_by for "how many per ..."; get_metrics for business numbers; query_data only with the needed columns,
  WHERE and COUNT/SUM/GROUP BY (never SELECT *). Table fields are listed in get_context.
- Then answer in your own reply (text, or a short markdown table of just the matching items).
- If the owner wants to SEE them as a card, call browse_records with where={ids:[...]} and fields=[...] of only those items,
  never the whole table.
Never call two card tools for one message unless asked. Housekeeping (resolve_question, mark_contacted, complete_task,
undo_change) has no card: batch it (one resolve_question with question_ids / all_open).

SAVE what the owner tells you, in the same turn, with ONE capture call per message, choosing the right type:
- a visit or sale that happened -> transaction; contact details or a new person -> customer_info
- what a person likes, dislikes, must have, will never accept, wants to reach, cannot do, their health, family, style or
  situation -> fact WITH customer and fact_kind (kinds are listed in get_context); add key for comparable things
  (budget, floor, area) so a later change is noticed. How two people are linked (spouse, parent, referred by) -> relation.
- a price or cost of what you sell -> offering; a one-off cost -> expense
- rent, salaries, subscriptions or any cost that repeats ("rent is now 120/month") -> expense with recurring="monthly"
- a promise, follow-up, meeting, call, delivery or money someone will pay on a date -> task (text, date, time, staff, customer, amount)
- a goal / a rule / a lasting fact / a decision taken / a team change -> goal / policy / fact / decision / staff
Do not capture questions, ideas the owner is only considering (unless they ask to remember them), or what tools already know.
Never say "saved" without the capture result. Files -> add_data; photos -> import_records; old chats -> import_chat_history.
Cleaning: browse_records, then edit_records / change_schema; destructive or bulk changes return a preview: ask, then confirm=true.
PEOPLE: before meeting or writing to someone call get_person_brief (quietly if they did not ask to see it; it masks health and
documents unless show_sensitive=true, which you use only when the owner asks for those by name). "Forget / delete everything about
X" -> edit_records action=forget_person (preview, then confirm=true); "what do you hold about X" -> action=export_person.
Design wishes -> update_design. New kinds of things to track -> change_schema.
OTHER APPS (calendar, Gmail/Outlook, Square/Shopify/Stripe, booking tools) through their own connectors:
- What you read there and save here gets source=<app> and external_id=<its id>, so a second sync never stores it twice and a
  moved appointment replaces the old one. Bring in batches with capture dry_run=true first; save after the owner agrees.
- get_context says when each app was last synced: read only what is newer.
- Before you write an email or message to a customer, or prepare for a meeting, call get_person_brief first (quietly).
"""


def M_(minor) -> dict:
    return {"$m": minor}


def _previous(name: str, s: dt.date, e: dt.date) -> tuple[dt.date, dt.date]:
    """The comparison period: the previous calendar month for month periods, else the same length before."""
    if name == "last_month":
        return M.month_bounds(M.shift_month(f"{s.year:04d}-{s.month:02d}", -1))
    if name == "this_month":
        ps = M.month_bounds(M.shift_month(f"{s.year:04d}-{s.month:02d}", -1))[0]
        return ps, ps + (e - s)
    length = (e - s).days + 1
    return s - dt.timedelta(days=length), s - dt.timedelta(days=1)


def _period(name: str, today: dt.date) -> tuple[dt.date, dt.date]:
    y = today - dt.timedelta(days=1)
    if name == "this_month":
        return today.replace(day=1), max(today.replace(day=1), y)
    if name == "last_30d":
        return today - dt.timedelta(days=30), y
    if name == "last_90d":
        return today - dt.timedelta(days=90), y
    if name == "last_12m":
        return today - dt.timedelta(days=365), y
    return M.month_bounds(M.last_full_month(today))


COUNTRY_TZ = {  # single-zone countries; others can set the `timezone` setting
    "RO": "Europe/Bucharest", "MD": "Europe/Chisinau", "FR": "Europe/Paris", "BE": "Europe/Brussels", "LU": "Europe/Luxembourg",
    "CH": "Europe/Zurich", "DE": "Europe/Berlin", "AT": "Europe/Vienna", "NL": "Europe/Amsterdam", "IT": "Europe/Rome",
    "PL": "Europe/Warsaw", "CZ": "Europe/Prague", "SK": "Europe/Bratislava", "HU": "Europe/Budapest", "BG": "Europe/Sofia",
    "GR": "Europe/Athens", "IE": "Europe/Dublin", "GB": "Europe/London", "UK": "Europe/London", "DK": "Europe/Copenhagen",
    "SE": "Europe/Stockholm", "NO": "Europe/Oslo", "FI": "Europe/Helsinki", "HR": "Europe/Zagreb", "SI": "Europe/Ljubljana",
    "RS": "Europe/Belgrade", "MC": "Europe/Monaco", "IR": "Asia/Tehran", "TR": "Europe/Istanbul", "AE": "Asia/Dubai",
}


class ContextEngine(WriteOps, EditOps, OnboardingOps, PeopleOps):
    def __init__(self, source: str | Path | Store, today: Optional[dt.date] = None):
        """`source` is where the memory file lives: a local path or any Store (Drive, OneDrive...).

        The engine works on an in-memory copy. Each tool call first picks up a newer stored version
        if another device wrote one, and afterwards saves the file only if the call changed data.
        """
        self.store = source if isinstance(source, Store) else LocalStore(source)
        self._lock = threading.RLock()
        self._today = today
        data, self._version = self.store.load()
        self.conn = open_memory(data)
        self._mark_clean()

    # ------------------------------------------------------------------ storage
    def readonly(self) -> sqlite3.Connection:
        """SELECT-only snapshot for query_data and custom metrics."""
        return readonly_copy(self.conn)

    def _fingerprint(self) -> tuple[int, int]:
        return self.conn.total_changes, self.conn.execute("PRAGMA schema_version").fetchone()[0]

    def _mark_clean(self) -> None:
        self._clean = self._fingerprint()

    def _dirty(self) -> bool:
        return self._fingerprint() != self._clean

    def _reload(self) -> None:
        data, version = self.store.load()
        if data is None and self._version is not None:
            raise RuntimeError(self.store.missing_message())
        load_into(self.conn, data)
        self._version = version
        self._mark_clean()

    def _sync(self) -> None:
        if self.store.version() != self._version:
            self._reload()

    def _persist(self) -> None:
        self._version = self.store.save(self.conn.serialize(), self._version)
        self._mark_clean()

    # ---------------------------------------------------------------- settings
    @property
    def today(self) -> dt.date:
        """The business's own date: a hosted server runs in UTC, but a café in Bucharest lives on Bucharest time."""
        if self._today:
            return self._today
        tz = self.setting("timezone") or COUNTRY_TZ.get((self.setting("country") or "").upper())
        if tz:
            try:
                from zoneinfo import ZoneInfo
                return dt.datetime.now(ZoneInfo(tz)).date()
            except Exception:  # noqa: BLE001 - unknown zone or no tz database: fall back to the machine's date
                pass
        return dt.date.today()

    def setting(self, key: str, default=None):
        return get_setting(self.conn, key, default)

    @property
    def lang(self) -> str:
        return self.setting("locale", "en")

    @property
    def currency(self) -> str:
        return self.setting("currency", "EUR")

    @property
    def is_setup(self) -> bool:
        return bool(self.setting("vertical")) and bool(schema.entities(self.conn))

    def label(self, role: str, plural: bool = True, lang: str = "en") -> str:
        spec = schema.primary(self.conn, role)
        return schema.label(spec, lang, plural) if spec else role

    def money(self, minor) -> str:
        return i18n.money(minor, self.currency, "en")

    def has_data(self) -> bool:
        return self.conn.execute("SELECT 1 FROM v_transactions LIMIT 1").fetchone() is not None

    # ------------------------------------------------------------------- KPIs
    def kpis(self, metrics: list[str], cur: dict, prev: dict, start: dt.date, end: dt.date) -> dict:
        items = []
        customs = {m["name"]: m for m in M.custom_metrics(self.conn)}
        for m in metrics:
            if m in KPI_FMT:
                fmt, good = KPI_FMT[m]
                if m == "no_show_rate":
                    if cur.get(m) is None:
                        continue
                    delta = round(cur[m] - prev[m], 1) if prev.get(m) is not None else None
                else:
                    delta = M.pct_change(cur[m], prev[m])
                items.append(kpi(m, cur[m], prev.get(m), fmt, good, delta))
            elif m in customs:
                ro = self.readonly()
                length = (end - start).days + 1
                v = M.eval_metric(ro, customs[m]["sql"], start, end)
                pv = M.eval_metric(ro, customs[m]["sql"], start - dt.timedelta(days=length), start - dt.timedelta(days=1))
                it = kpi(m, v, pv, customs[m]["format"], "up", M.pct_change(v or 0, pv or 0))
                it["label_text"] = customs[m]["label"]
                items.append(it)
        return block("kpis", items=items)

    # ------------------------------------------------------------------ brief
    def get_business_brief(self) -> dict:
        if not self.is_setup or not self.has_data():
            return self.get_started()
        c, today = self.conn, self.today
        spec = design.get_spec(c)
        lm = M.last_full_month(today)
        pm = M.shift_month(lm, -1)
        s, e = M.month_bounds(lm)
        cur, prev = M.period_summary(c, s, e), M.period_summary(c, *M.month_bounds(pm))
        stats = M.customer_stats(c, today)
        sections = {
            "kpis": lambda: self.kpis(spec["brief"]["kpis"], cur, prev, s, e),
            "attention": lambda: block("alerts", title=T("sec.attention"), items=self._attention(cur, prev, stats, lm, pm)),
            "memory": self._memory_block,
            "trend": lambda: self._trend_block(lm),
            "hours": lambda: self._hours_block(cur),
            "staff": lambda: self._staff_block(cur),
            "offerings": self._offerings_block,
            "decisions": self._decisions_block,
            "notes": self._notes_block,
            "quality": self._quality_block,
        }
        blocks = [sections[k]() for k in spec["brief"]["sections"] if k in sections]
        span = c.execute("SELECT MIN(date) a, MAX(date) b, COUNT(*) n FROM v_transactions").fetchone()
        parties = c.execute("SELECT COUNT(*) FROM v_parties").fetchone()[0]
        self._mark_used("brief")
        return view("get_business_brief", T("view.brief"), blocks,
                    subtitle=T("sub.brief", month=MO(lm), prev=MO(pm), n=N(span["n"]), customers=N(parties), start=D(span["a"]), end=D(span["b"])),
                    hints=["Summarise the 2-3 most important points in the owner's language; the card shows the details.",
                           "Capture any new fact the owner mentions (capture tool)."])

    def _attention(self, cur: dict, prev: dict, stats: list, lm: str, pm: str) -> list[dict]:
        out = []
        c, today = self.conn, self.today
        last = c.execute("SELECT MAX(date) b FROM v_transactions").fetchone()["b"]
        stale = (today - dt.date.fromisoformat(last)).days if last else None
        if stale and stale > 7:
            out.append({**T("alert.stale", days=N(stale)), "level": "warn"})
        if cur["revenue"] < prev["revenue"]:
            for key, label in (("by_staff", "staff"), ("by_offering", "offering")):
                drivers = M.change_drivers(cur, prev, key)
                if drivers and drivers[0]["delta"] < 0:
                    out.append({**T(f"alert.drop_{label}", name=drivers[0]["name"], delta=M_(drivers[0]["delta"]), month=MO(lm), prev=MO(pm)), "level": "warn"})
                    break
        from .verticals import GENERIC, PACKS
        segs = PACKS.get(self.setting("vertical"), GENERIC)["segments"]
        lapsed = M.segment(stats, "lapsed_regulars") if "lapsed_regulars" in segs else []
        by_staff = M.lapsed_by_staff(stats)
        for st in M.staff_activity(c, today):
            if not st["active"] and (today - dt.date.fromisoformat(st["last"])).days < 150:
                out.append({**T("alert.staff_inactive", name=st["name"], since=D(st["last"]), n=N(by_staff.get(st["name"], 0))), "level": "warn"})
        if lapsed:
            out.append({**T("alert.lapsed", n=N(len(lapsed)), spend=M_(sum(x.total_spend for x in lapsed))), "level": "warn"})
        if cur.get("no_show_rate") is not None and prev.get("no_show_rate") is not None and cur["no_show_rate"] > prev["no_show_rate"] + 2:
            out.append({**T("alert.no_show_up", cur=P(cur["no_show_rate"]), prev=P(prev["no_show_rate"]), n=N(cur["no_shows"])), "level": "warn"})
        for r in c.execute("SELECT decision, review_on FROM decisions WHERE status='open' AND review_on <= ?",
                           ((today + dt.timedelta(days=7)).isoformat(),)):
            out.append({**T("alert.decision_due", decision=r["decision"], date=D(r["review_on"])), "level": "info"})
        n_over = c.execute("SELECT COUNT(*) FROM tasks WHERE status='open' AND due < ?", (today.isoformat(),)).fetchone()[0]
        n_today = c.execute("SELECT COUNT(*) FROM tasks WHERE status='open' AND due = ?", (today.isoformat(),)).fetchone()[0]
        if n_over or n_today:
            out.append({**T("alert.tasks", today=N(n_today), overdue=N(n_over)), "level": "warn" if n_over else "info"})
        if not cur["expenses_complete"]:
            out.append({**T("alert.costs_missing", month=MO(lm)), "level": "info"})
        n_q = c.execute("SELECT COUNT(*) FROM questions WHERE status='open'").fetchone()[0]
        if n_q:
            out.append({**T("alert.questions", n=N(n_q)), "level": "info"})
        if cur["revenue"] > prev["revenue"]:
            out.append({**T("alert.revenue_up", pct=P(M.pct_change(cur["revenue"], prev["revenue"]))), "level": "good"})
        return out

    def _memory_block(self) -> Optional[dict]:
        rows = self.conn.execute("SELECT kind, section, text, created_at FROM knowledge WHERE origin!='file' ORDER BY id DESC LIMIT 8").fetchall()
        if not rows:
            return None
        return block("notes", title=T("sec.memory"), items=[{"kind": r["kind"], "title": r["section"], "text": r["text"], "date": r["created_at"][:10]} for r in rows])

    def _trend_block(self, lm: str) -> dict:
        tr = M.monthly_trend(self.conn, lm, 6)
        return block("chart", title=T("sec.trend"), kind="bar", fmt="money", label_fmt="month",
                     series=[{"label": x["month"], "value": x["revenue"]} for x in tr])

    def _hours_block(self, cur: dict) -> Optional[dict]:
        if not cur["by_hour"]:
            return None
        return block("chart", title=T("sec.hours"), kind="bar", fmt="money", label_fmt="hour",
                     series=[{"label": f"{h:02d}", "value": v} for h, v in cur["by_hour"].items()])

    def _staff_block(self, cur: dict) -> Optional[dict]:
        staff = M.staff_activity(self.conn, self.today)
        if not staff:
            return None
        rev = {x["name"]: x["revenue"] for x in cur["by_staff"]}
        rows = [{"name": s["name"], "status": T("status.active" if s["active"] else "status.inactive"), "last": s["last"],
                 "revenue": rev.get(s["name"], 0)} for s in sorted(staff, key=lambda s: -rev.get(s["name"], 0))]
        return block("table", title=T("sec.staff"), columns=[col("name"), col("status", "badge"), col("last", "date"), col("revenue", "money")], rows=rows)

    def _offerings_block(self) -> Optional[dict]:
        rows = [{"name": m["name"], "price": m["price"], "unit_cost": m["unit_cost"], "margin": m["margin_pct"]} for m in M.offering_margins(self.conn)]
        if not rows:
            return None
        return block("table", title=T("sec.offerings"), columns=[col("name"), col("price", "money"), col("unit_cost", "money"), col("margin", "percent")],
                     rows=rows, collapsed=len(rows) > 8)

    def _decisions_block(self) -> Optional[dict]:
        rows = [dict(r) for r in self.conn.execute("SELECT decided_on, decision, expected_outcome, review_on FROM decisions WHERE status='open' ORDER BY review_on")]
        if not rows:
            return None
        return block("table", title=T("sec.decisions"), columns=[col("decision"), col("decided_on", "date"), col("expected_outcome"), col("review_on", "date")], rows=rows)

    def _notes_block(self) -> Optional[dict]:
        rows = self.conn.execute("SELECT section, text FROM knowledge WHERE origin='file' ORDER BY id LIMIT 12").fetchall()
        if not rows:
            return None
        return block("notes", title=T("sec.notes"), items=[{"kind": None, "title": r["section"], "text": r["text"][:400]} for r in rows], collapsed=True)

    def _quality_block(self) -> Optional[dict]:
        issues = M.data_quality(self.conn)
        if not issues:
            return None
        return block("alerts", title=T("sec.quality"), items=[{**T(f"dq.{i['kind']}", n=N(i["count"])), "level": "info"} for i in issues])

    # -------------------------------------------------------- silent context
    def get_context(self) -> str:
        """Background context for the assistant: no card. Call at the start of a conversation."""
        from .i18n import t as tr
        if not self.is_setup:
            v = self.get_started()
            return ("NOT SET UP YET. Do not show cards unless the owner wants to start. Next onboarding step: "
                    + tr(v["subtitle"]["t"], "en") + ". Offer to set up In The Know (get_started shows the guided card).")
        c, today = self.conn, self.today
        lines = [f"Business: {self.setting('name')} ({self.setting('vertical')}), {self.setting('city') or ''} · language {self.lang} · "
                 f"currency {self.currency} · today {today.isoformat()}"]
        tracked = {schema.table(n): (schema.label(sp, "en", False).lower(), schema.label(sp, "en", True).lower())
                   for n, sp in schema.entities(c).items()}
        tracked.update({"raw_files": ("file imported", "files imported"), "knowledge": ("note", "notes"),
                        "decisions": ("decision", "decisions"), "tasks": ("task", "tasks")})
        marks = {t_: c.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t_}").fetchone()[0] for t_ in tracked}
        last = json.loads(self.setting("context_marks", "null") or "null")
        if last is not None:
            changes = []
            for t_, (one, many) in tracked.items():
                k = c.execute(f"SELECT COUNT(*) FROM {t_} WHERE id > ?", (last.get(t_, 0),)).fetchone()[0]
                if k:
                    changes.append(f"{k} new {one if k == 1 else many}")
            lines.append("Since the last check: " + (", ".join(changes) if changes else "nothing new."))
        syncs = [(r["key"][10:], r["value"].split("|")[0]) for r in c.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'sync_last:%' ORDER BY key")]
        if syncs:
            lines.append("Last synced from other apps: " + ", ".join(f"{k} {d}" for k, d in syncs)
                         + " (read only newer items; use source + external_id).")
        if self.has_data():
            lm = M.last_full_month(today)
            pm = M.shift_month(lm, -1)
            cur, prev = M.period_summary(c, *M.month_bounds(lm)), M.period_summary(c, *M.month_bounds(pm))
            mtd = M.period_summary(c, today.replace(day=1), today - dt.timedelta(days=1)) if today.day > 1 else None
            spec = design.get_spec(c)
            nums = []
            for m in spec["brief"]["kpis"]:
                if m in KPI_FMT and cur.get(m) is not None:
                    fmt = KPI_FMT[m][0]
                    f = (lambda x: self.money(x)) if fmt == "money" else ((lambda x: f"{x}%") if fmt == "percent" else str)
                    nums.append(f"{m.replace('_', ' ')} {f(cur[m])} (prev {f(prev[m]) if prev.get(m) is not None else '—'})")
            lines.append(f"Last full month {lm} vs {pm}: " + "; ".join(nums))
            if mtd:
                lines.append(f"This month so far: revenue {self.money(mtd['revenue'])}, {mtd['transactions']} transactions.")
            alerts = self._attention(cur, prev, M.customer_stats(c, today), lm, pm)
            if alerts:
                lines.append("Needs attention: " + " | ".join(tr(a["t"], "en", a.get("p"), self.currency) for a in alerts if a.get("level") != "good"))
        else:
            lines.append("No sales/visits recorded yet.")
        tasks = c.execute("SELECT * FROM tasks WHERE status='open' AND (due IS NULL OR due <= ?) ORDER BY due",
                          ((today + dt.timedelta(days=7)).isoformat(),)).fetchall()
        if tasks:
            lines.append("Open tasks: " + " | ".join(
                f"#{t_['id']} {t_['text']}" + (f" (due {t_['due']}{' ' + t_['due_time'] if t_['due_time'] else ''}"
                                               f"{', OVERDUE' if t_['due'] and t_['due'] < today.isoformat() else ''})" if t_['due'] else "")
                + (f" [{t_['assignee']}]" if t_["assignee"] else "") + (f" {self.money(t_['amount'])}" if t_["amount"] is not None else "")
                for t_ in tasks))
        rc = c.execute("SELECT category, amount, start_month FROM recurring_costs WHERE end_month IS NULL").fetchall()
        if rc:
            lines.append("Recurring monthly costs: " + ", ".join(f"{r['category']} {self.money(r['amount'])} (since {r['start_month']})" for r in rc))
        mem = c.execute("SELECT kind, text FROM knowledge WHERE origin!='file' ORDER BY id DESC LIMIT 6").fetchall()
        if mem:
            lines.append("Owner said earlier: " + " | ".join(f"[{r['kind']}] {r['text']}" for r in mem))
        goals = c.execute("SELECT text FROM knowledge WHERE kind='goal' OR lower(section) LIKE '%obiectiv%' OR lower(section) LIKE '%objectif%' "
                          "OR lower(section) LIKE '%ziel%' OR lower(section) LIKE '%goal%' LIMIT 3").fetchall()
        if goals:
            lines.append("Goals: " + " | ".join(g["text"][:160] for g in goals))
        nq = c.execute("SELECT COUNT(*) FROM questions WHERE status='open'").fetchone()[0]
        if nq:
            lines.append(f"{nq} open data question(s) (get_data_status when relevant).")
        self.purge_forgotten()
        lines += self._people_context()
        lines.append("Tables (fields): " + "; ".join(
            f"{n} [{s_['role']}]: " + ", ".join(f["name"] + (f"->{f['ref']}" if f["type"] == "ref" else "") for f in s_["fields"])
            for n, s_ in schema.entities(c).items()))
        lines.append("Answer the owner's actual message. Show a card only if they asked to SEE something (overview, a customer, "
                     "a period, this week's list, data status); for questions use get_metrics, find_records or query_data and reply in text.")
        if marks != last:                        # unchanged marks write nothing, so the file is not re-saved
            from .db import set_setting
            set_setting(c, "context_marks", json.dumps(marks))
        return "\n".join(lines)

    def get_metrics(self, metrics: Optional[list[str]] = None, period: str = "last_month", start: Optional[str] = None,
                    end: Optional[str] = None, by: Optional[str] = None) -> str:
        """Exact numbers for a quick question, as text (no card)."""
        if start and end:
            s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
            whole = s.day == 1 and e == M.month_bounds(f"{e.year:04d}-{e.month:02d}")[1] and (s.year, s.month) == (e.year, e.month)
            ps, pe = _previous("last_month" if whole else "custom", s, e)
        else:
            s, e = _period(period, self.today)
            ps, pe = _previous(period, s, e)
        cur, prev = M.period_summary(self.conn, s, e), M.period_summary(self.conn, ps, pe)
        metrics = metrics or ["revenue", "transactions", "net_profit"]
        out = [f"{s} → {e} (previous: {ps} → {pe})"]
        for m in metrics:
            if m not in KPI_FMT:
                out.append(f"{m}: unknown metric (use {', '.join(KPI_FMT)})")
                continue
            fmt = KPI_FMT[m][0]
            show = (lambda x: self.money(x)) if fmt == "money" else ((lambda x: "—" if x is None else f"{x}%") if fmt == "percent" else str)
            d = M.pct_change(cur[m], prev[m]) if fmt != "percent" else None
            out.append(f"- {m}: {show(cur[m])} (previous {show(prev[m])}{f', {d:+.1f}%' if d is not None else ''})")
        if by in ("offering", "staff"):
            out.append(f"By {by}: " + ", ".join(f"{x['name']} {self.money(x['revenue'])} ({x['count']})" for x in cur[f"by_{by}"][:12]))
        elif by == "category":
            out.append("Costs by category: " + ", ".join(f"{k} {self.money(v)}" for k, v in cur["expenses_by_category"].items()))
        elif by == "hour":
            out.append("Revenue by hour: " + ", ".join(f"{h}h {self.money(v)}" for h, v in cur["by_hour"].items()))
        if not cur["expenses_complete"] and any(m in ("net_profit", "expenses") for m in metrics):
            out.append("Note: costs are not recorded for every month of this period, so profit is approximate.")
        self._mark_used("review")
        return "\n".join(out)

    # ------------------------------------------------------------- action list
    def get_action_list(self, days: int = 7) -> dict:
        if not self.has_data():
            return self.get_started()
        c, today = self.conn, self.today
        stats = M.customer_stats(c, today)
        staff = {s["name"]: s for s in M.staff_activity(c, today)}
        lapsed = M.segment(stats, "lapsed_regulars")
        lapsed_ids = {s.id for s in lapsed}

        def recently(s, d_):
            return s.last_contacted and (today - dt.date.fromisoformat(s.last_contacted)).days < d_

        from .verticals import GENERIC, PACKS
        segs = PACKS.get(self.setting("vertical"), GENERIC)["segments"]
        due = [s for s in M.segment(stats, "due_soon") if s.id not in lapsed_ids and not recently(s, 14)]
        win = [s for s in lapsed if not recently(s, 30)] if "lapsed_regulars" in segs else []
        risk = [s for s in M.segment(stats, "at_risk") if s.id not in lapsed_ids and not recently(s, 14)]

        def rows(lst, n):
            return [{"_id": s.id, "name": s.name, "phone": s.phone, "last_visit": s.last_visit, "days": s.days_since_last,
                     "interval": s.avg_interval_days, "offering": s.favorite_offering, "spend": s.total_spend,
                     "staff": (s.last_staff or "—") + (" ⚑" if s.last_staff in staff and not staff[s.last_staff]["active"] else "")}
                    for s in lst[:n]]

        blocks = []
        if due:
            blocks.append(block("table", title=T("sec.due", n=N(len(due))), actions=["message", "contacted"],
                                columns=[col("name"), col("phone", "phone"), col("last_visit", "date"), col("interval", "number"),
                                         col("days", "days"), col("offering"), col("staff")], rows=rows(due, 12), more=max(0, len(due) - 12)))
        if win:
            blocks.append(block("table", title=T("sec.winback", n=N(len(win)), spend=M_(sum(s.total_spend for s in win))),
                                actions=["message", "contacted"],
                                columns=[col("name"), col("phone", "phone"), col("spend", "money"), col("last_visit", "date"),
                                         col("offering"), col("staff")], rows=rows(win, 8), more=max(0, len(win) - 8)))
        if risk:
            blocks.append(block("table", title=T("sec.at_risk", n=N(len(risk))), actions=["message", "contacted"],
                                columns=[col("name"), col("days", "days"), col("interval", "number"), col("offering")], rows=rows(risk, 8)))
        tasks = [dict(r) for r in c.execute("SELECT t.id AS _id, t.text AS task, t.due, t.due_time AS time, t.assignee, t.amount, p.name AS customer "
                                            "FROM tasks t LEFT JOIN v_parties p ON p.id=t.party_id WHERE t.status='open' AND (t.due IS NULL OR t.due <= ?) "
                                            "ORDER BY t.due IS NULL, t.due, t.due_time", ((today + dt.timedelta(days=days)).isoformat(),))]
        if tasks:
            for t_ in tasks:
                t_["overdue"] = bool(t_["due"] and t_["due"] < today.isoformat())
            blocks.insert(0, block("table", title=T("sec.tasks", n=N(len(tasks))), actions=["done"],
                                   columns=[col("task"), col("due", "date"), col("time"), col("assignee", "text", "col.staff"),
                                            col("customer", "text", "col.customer"), col("amount", "money")], rows=tasks))
        dec = [dict(r) for r in c.execute("SELECT decision, decided_on, expected_outcome, review_on FROM decisions WHERE status='open' AND review_on <= ?",
                                          ((today + dt.timedelta(days=days)).isoformat(),))]
        if dec:
            blocks.append(block("table", title=T("sec.decisions_due"), columns=[col("decision"), col("expected_outcome"), col("review_on", "date")], rows=dec))
        q = c.execute("SELECT COUNT(*) FROM questions WHERE status='open'").fetchone()[0]
        if q:
            blocks.append(block("alerts", items=[{**T("alert.questions", n=N(q)), "level": "info"}]))
        if not blocks:
            blocks.append(block("text", **T("actions.none")))
        return view("get_action_list", T("view.actions"), blocks, subtitle=T("sub.actions", days=N(days), today=D(today.isoformat())),
                    hints=["Offer to draft messages (get_person_brief first). ⚑ = their usual staff member is no longer active."])

    # ---------------------------------------------------------------- period
    def review_period(self, start: str, end: str) -> dict:
        s, e = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
        length = (e - s).days + 1
        ps, pe = s - dt.timedelta(days=length), s - dt.timedelta(days=1)
        if s.day == 1 and e == M.month_bounds(f"{e.year:04d}-{e.month:02d}")[1]:
            months = (e.year - s.year) * 12 + e.month - s.month + 1
            ps = M.month_bounds(M.shift_month(f"{s.year:04d}-{s.month:02d}", -months))[0]
        cur, prev = M.period_summary(self.conn, s, e), M.period_summary(self.conn, ps, pe)
        metrics = ["revenue", "receipts" if M.receipts_expr(self.conn) else "transactions", "unique_customers", "new_customers",
                   "avg_ticket", "cost_of_goods", "expenses", "net_profit"] + (["no_show_rate"] if cur.get("no_show_rate") is not None else [])
        by_off = sorted(M.change_drivers(cur, prev, "by_offering"), key=lambda x: -x["current"])
        counts = {x["name"]: x["count"] for x in cur["by_offering"]}
        blocks = [self.kpis(metrics, cur, prev, s, e),
                  block("table", title=T("sec.by_offering"), columns=[col("name"), col("revenue", "money"), col("count", "number"),
                                                                        col("previous", "money"), col("delta", "money")],
                        rows=[{"name": x["name"], "revenue": x["current"], "count": counts.get(x["name"], 0), "previous": x["previous"],
                               "delta": x["delta"]} for x in by_off])]
        if cur["by_staff"] or prev["by_staff"]:
            blocks.append(block("table", title=T("sec.by_staff"), columns=[col("name"), col("revenue", "money"), col("previous", "money"), col("delta", "money")],
                                rows=[{"name": x["name"], "revenue": x["current"], "previous": x["previous"], "delta": x["delta"]}
                                      for x in sorted(M.change_drivers(cur, prev, "by_staff"), key=lambda x: -x["current"])]))
        if cur["by_hour"] and length <= 92:
            blocks.append(self._hours_block(cur))
        if cur["expenses_by_category"]:
            blocks.append(block("kv", title=T("sec.costs"), items=[{"raw": k, "v": v, "fmt": "money"} for k, v in cur["expenses_by_category"].items()]))
        q = []
        if not cur["expenses_complete"]:
            q.append({**T("dq.costs_incomplete"), "level": "info"})
        if cur["unit_cost_missing"]:
            q.append({**T("dq.unit_cost_missing_rows", n=N(cur["unit_cost_missing"])), "level": "info"})
        if q:
            blocks.append(block("alerts", title=T("sec.quality"), items=q))
        self._mark_used("review")
        return view("review_period", T("view.period"), blocks, subtitle=T("sub.period", start=D(s.isoformat()), end=D(e.isoformat()),
                                                                               pstart=D(ps.isoformat()), pend=D(pe.isoformat())))

    # ------------------------------------------------------------- customers
    def find_customers(self, segment: str, min_visits: int = 3, min_days_since_last_visit: int = 60, limit: int = 15) -> dict:
        stats = M.customer_stats(self.conn, self.today)
        found = M.segment(stats, segment, min_visits, min_days_since_last_visit)
        rows = [{"_id": s.id, "name": s.name, "phone": s.phone, "visits": s.visits, "spend": s.total_spend, "last_visit": s.last_visit,
                 "days": s.days_since_last, "interval": s.avg_interval_days, "offering": s.favorite_offering, "staff": s.last_staff}
                for s in found[:limit]]
        blocks = [block("kv", items=[{"t": "kv.count", "v": len(found), "fmt": "number"},
                                     {"t": "kv.total_spend", "v": sum(s.total_spend for s in found), "fmt": "money"}]),
                  block("table", columns=[col("name"), col("phone", "phone"), col("visits", "number"), col("spend", "money"), col("last_visit", "date"),
                                          col("days", "days"), col("interval", "number"), col("offering"), col("staff")],
                        rows=rows, actions=["message", "contacted"], more=max(0, len(found) - limit))]
        if segment == "lapsed_regulars" and found:
            by = M.lapsed_by_staff(stats, min_visits, min_days_since_last_visit)
            blocks.append(block("chart", title=T("sec.lapsed_by_staff"), kind="bar", fmt="number", label_fmt="text",
                                series=[{"label": k, "value": v} for k, v in by.most_common()]))
        return view("find_customers", T(f"seg.{segment}"), blocks, subtitle=T(f"segdesc.{segment}", visits=N(min_visits), days=N(min_days_since_last_visit)),
                    hints=["Use get_person_brief before writing to a customer."])

    def get_person_brief(self, name_or_phone: str, show_sensitive: bool = False) -> dict:
        cid = self.find_party(name_or_phone)
        if cid is None:
            return view("get_person_brief", T("view.customer_missing"), [block("text", **T("customer.not_found", q=name_or_phone))],
                        hints=["Search with part of the name, or use find_customers."])
        if isinstance(cid, list):
            rows = [{"name": r["name"], "phone": r["phone"], "last_visit": r["last_visit"], "visits": r["visits"]} for r in cid]
            return view("get_person_brief", T("view.customer_ambiguous"),
                        [block("table", columns=[col("name"), col("phone", "phone"), col("visits", "number"), col("last_visit", "date")], rows=rows)],
                        hints=["Ask the owner which one (by phone or full name)."])
        st = next(x for x in M.customer_stats(self.conn, self.today) if x.id == cid)
        pspec = schema.primary(self.conn, "party")
        rec = dict(self.conn.execute(f"SELECT * FROM {schema.table(pspec['name'])} WHERE id=?", (cid,)).fetchone())
        nf = schema.role_field(pspec, "notes")
        notes = [ln for ln in (rec.get(nf["name"]) or "").split("\n") if ln.strip()] if nf else []
        extra = {f["name"]: rec[f["name"]] for f in pspec["fields"] if not f.get("role_attr") and rec.get(f["name"]) not in (None, "")}
        extra.update(json.loads(rec.get("_extra") or "{}"))
        aliases = [r["key"][2:] for r in self.conn.execute("SELECT key FROM aliases WHERE entity=? AND record_id=? AND key LIKE 'n:%'", (pspec["name"], cid))]
        warnings = []
        staff = {x["name"]: x for x in M.staff_activity(self.conn, self.today)}
        if st.favorite_staff in staff and not staff[st.favorite_staff]["active"]:
            warnings.append(T("warn.staff_left", name=st.favorite_staff, since=D(staff[st.favorite_staff]["last"])))
        if st.avg_interval_days and st.days_since_last is not None:
            ratio = st.days_since_last / st.avg_interval_days
            if ratio > 1.5:
                warnings.append(T("warn.overdue", days=N(st.days_since_last), ratio=N(round(ratio, 1))))
            elif ratio >= 0.8:
                warnings.append(T("warn.due_in", days=N(max(0, round(st.avg_interval_days - st.days_since_last)))))
        if len(aliases) > 1:
            warnings.append(T("warn.merged", n=N(len(aliases))))
        if st.last_contacted:
            warnings.append(T("warn.contacted", date=D(st.last_contacted)))
        facts = [{"t": "fact.visits", "v": st.visits, "fmt": "number"}, {"t": "fact.spend", "v": st.total_spend, "fmt": "money"},
                 {"t": "fact.last", "v": st.last_visit, "fmt": "date"}, {"t": "fact.rhythm", "v": st.avg_interval_days, "fmt": "number"},
                 {"t": "fact.offering", "v": st.favorite_offering, "fmt": "text"}, {"t": "fact.staff", "v": st.favorite_staff, "fmt": "text"},
                 {"t": "fact.first_seen", "v": st.first_seen, "fmt": "date"}]
        if st.no_shows:
            facts.append({"t": "fact.no_shows", "v": st.no_shows, "fmt": "number"})
        visits = self.conn.execute("SELECT date, offering, staff, amount, status FROM v_transactions WHERE party_id=? ORDER BY date DESC, time DESC LIMIT 12", (cid,)).fetchall()
        blocks = [block("entity", id=cid, name=st.name, subtitle=st.phone, status=st.status, facts=facts, notes=notes, extra=extra,
                        warnings=warnings, actions=["message", "contacted"]),
                  block("timeline", title=T("sec.history"), items=[{"date": v["date"], "title": v["offering"], "sub": v["staff"],
                                                                     "amount": v["amount"], "status": v["status"]} for v in visits])]
        blocks[1:1] = self._people_blocks(cid, show_sensitive)
        tasks = [dict(r) for r in self.conn.execute("SELECT id AS _id, text AS task, due, amount FROM tasks WHERE party_id=? AND status='open' "
                                                    "ORDER BY due IS NULL, due", (cid,))]
        if tasks:
            blocks.insert(1, block("table", title=T("sec.open_items"), actions=["done"],
                                   columns=[col("task"), col("due", "date"), col("amount", "money")], rows=tasks))
        for cs in schema.by_role(self.conn, "custom"):           # related records, e.g. the customer's pets
            ref = next((f for f in cs["fields"] if f["type"] == "ref" and f.get("ref") == pspec["name"]), None)
            if ref:
                rows = [dict(r) for r in self.conn.execute(f"SELECT * FROM {schema.table(cs['name'])} WHERE {schema.column(ref)}=?", (cid,))]
                if rows:
                    shown = [f["name"] for f in cs["fields"] if f["type"] != "ref"][:5]
                    blocks.append(block("table", title={"raw": schema.label(cs, self.lang, True)},
                                        columns=[{"k": k, "raw": k.replace("_", " "), "fmt": "text"} for k in shown],
                                        rows=[{k: r.get(k) for k in shown} for r in rows]))
        hints = [f"Message tone: {self._tone()}. Mention their usual offering/staff; if the staff left, introduce someone else.",
                 "Facts marked ? were stated by the person (not confirmed), ~ were inferred. Quote the source when the owner asks why."]
        if any(b["type"] == "facts" and b.get("masked") for b in blocks):
            hints.append("Sensitive facts are hidden; call again with show_sensitive=true only if the owner asks for them.")
        if any(b["type"] == "facts" and b.get("missing") for b in blocks):
            hints.append("'Not known yet' lists key facts missing for this person: offer to ask them at the next contact.")
        return view("get_person_brief", {"raw": st.name}, blocks, subtitle=T(f"status.{st.status}"), hints=hints)

    def get_customer_context(self, name_or_phone: str) -> dict:
        """Old name of get_person_brief (kept for saved prompts and Project instructions)."""
        return self.get_person_brief(name_or_phone)

    def _tone(self) -> str:
        from .verticals import GENERIC, PACKS
        return PACKS.get(self.setting("vertical"), GENERIC)["tone"].get(self.lang, "friendly")

    def find_party(self, q: str):
        spec = schema.primary(self.conn, "party")
        if spec is None:
            return None
        ent = spec["name"]
        digits = re.sub(r"\D", "", q)
        ph = phone_key(q, self.setting("country", "RO")) if len(digits) >= 6 else None
        if ph:
            r = self.conn.execute("SELECT record_id FROM aliases WHERE entity=? AND key=?", (ent, f"p:{ph}")).fetchone()
            if r:
                return r["record_id"]
        k = name_key(q)
        r = self.conn.execute("SELECT record_id FROM aliases WHERE entity=? AND key=?", (ent, f"n:{k}")).fetchone()
        if r:
            return r["record_id"]
        tokens = set(k.split())
        hits = {a["record_id"] for a in self.conn.execute("SELECT key, record_id FROM aliases WHERE entity=? AND key LIKE 'n:%'", (ent,))
                if tokens and tokens <= set(a["key"][2:].split())}
        if len(hits) == 1:
            return next(iter(hits))
        if len(hits) > 1:
            return [dict(r) for r in self.conn.execute(
                f"SELECT id, name, phone, visits, last_visit FROM v_customers WHERE id IN ({','.join('?' * len(hits))}) ORDER BY last_visit DESC", list(hits))][:10]
        return None

    # ------------------------------------------------------------ data status
    def get_data_status(self) -> dict:
        c = self.conn
        files = []
        for f in c.execute("SELECT id, name, entity, report, ingested_at FROM raw_files ORDER BY id"):
            rep = json.loads(f["report"] or "{}")
            es = schema.entity(c, f["entity"]) if f["entity"] and f["entity"] != "knowledge" else None
            ent = schema.label(es, self.lang, True) if es else ({"knowledge": "📝"}.get(f["entity"]) or "?")
            files.append({"id": f["id"], "name": f["name"], "entity": ent, "loaded": rep.get("rows_loaded", 0),
                          "duplicate": rep.get("rows_duplicate", 0), "flagged": rep.get("rows_flagged", 0),
                          "range": " → ".join(rep.get("date_range") or []) or None})
        changes = [{"id": r["id"], "op": r["op"], **json.loads(r["payload"]), "status": r["status"],
                    "undoable": r["op"] in ("add_field", "create_entity") and r["status"] == "applied"}
                   for r in c.execute("SELECT * FROM schema_changes WHERE origin!='pack' ORDER BY id DESC LIMIT 12")]
        ents = [{"name": n, "role": s["role"], "label": schema.label(s, self.lang, True), "fields": [schema_field_view(f) for f in s["fields"]],
                 "rows": c.execute(f"SELECT COUNT(*) FROM {schema.table(n)}").fetchone()[0]} for n, s in schema.entities(c).items()]
        qs = [{"id": r["id"], "kind": r["kind"], "text": r["text"], "payload": json.loads(r["payload"])}
              for r in c.execute("SELECT * FROM questions WHERE status='open' ORDER BY id LIMIT 30")]
        chat = c.execute("SELECT COUNT(*) FROM v_transactions WHERE source IN ('chat','history','photo')").fetchone()[0]
        blocks = [block("kv", items=[{"t": "kv.files", "v": len(files), "fmt": "number"},
                                     {"t": "kv.transactions", "v": c.execute("SELECT COUNT(*) FROM v_transactions").fetchone()[0], "fmt": "number"},
                                     {"t": "kv.from_chat", "v": chat, "fmt": "number"},
                                     {"t": "kv.customers", "v": c.execute("SELECT COUNT(*) FROM v_parties").fetchone()[0], "fmt": "number"},
                                     {"t": "kv.notes", "v": c.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0], "fmt": "number"}]),
                  block("table", title=T("sec.files"), columns=[col("id", "number"), col("name"), col("entity"), col("loaded", "number"),
                                                                 col("duplicate", "number"), col("flagged", "number"), col("range")], rows=files) if files else None,
                  block("schema", title=T("sec.schema"), entities=ents, changes=changes),
                  block("questions", title=T("sec.questions", n=N(len(qs))), items=qs)]
        return view("get_data_status", T("view.data"), blocks,
                    hints=["Resolve questions with resolve_question / merge_customers / capture(type=offering) / map_columns.",
                           "Schema changes can be undone with undo_change(change_id)."])

    # ----------------------------------------------------------- custom views
    def show_view(self, name: str) -> dict:
        spec = design.get_spec(self.conn)
        v = spec["views"].get(name)
        if v is None:
            return view("show_view", T("view.no_view"), [block("text", **T("view.available", names=", ".join(spec["views"]) or "—"))],
                        hints=["Create views with update_design(views={...})."])
        blocks = []
        for b in v["blocks"]:
            s, e = _period(b.get("period", "last_month"), self.today)
            cur = M.period_summary(self.conn, s, e)
            prev = M.period_summary(self.conn, *_previous(b.get("period", "last_month"), s, e))
            title = b.get("title")
            title = ({"raw": title.get(self.lang) or title.get("en") or next(iter(title.values()))} if isinstance(title, dict)
                     else {"raw": title} if title else None)
            if b["type"] == "kpis":
                k = self.kpis(b["metrics"], cur, prev, s, e)
                k["title"] = title
                blocks.append(k)
            elif b["type"] == "chart":
                blocks.append(self._chart(b, cur, s, e, title))
            elif b["type"] == "table":
                blocks.append(self._source_table(b["source"], b.get("limit", 10), title))
            elif b["type"] == "alerts":
                stats = M.customer_stats(self.conn, self.today)
                lm = M.last_full_month(self.today)
                blocks.append(block("alerts", title=title or T("sec.attention"), items=self._attention(cur, prev, stats, lm, M.shift_month(lm, -1))))
        t = v["title"]
        t = (t.get(self.lang) or t.get("en")) if isinstance(t, dict) else t
        return view("show_view", {"raw": t}, [b for b in blocks if b], subtitle=T("sub.custom_view"))

    def _chart(self, b: dict, cur: dict, s: dt.date, e: dt.date, title) -> dict:
        metric, by = b.get("metric", "revenue"), b["by"]
        fmt = "money" if metric != "transactions" else "number"
        if by == "month":
            n = 12 if b.get("period") == "last_12m" else 6
            series = [{"label": x["month"], "value": x["revenue"] if metric != "transactions" else x["count"]}
                      for x in M.monthly_trend(self.conn, f"{e.year:04d}-{e.month:02d}", n)]
            lf = "month"
        elif by in ("offering", "staff"):
            series = [{"label": x["name"], "value": x["revenue"] if metric != "transactions" else x["count"]} for x in cur[f"by_{by}"][:12]]
            lf = "text"
        elif by == "hour":
            series = [{"label": f"{h:02d}", "value": v} for h, v in cur["by_hour"].items()]
            lf, fmt = "hour", "money"
        elif by == "weekday":
            agg = [0] * 7
            for r in self.conn.execute("SELECT date, amount FROM v_transactions WHERE counted=1 AND date BETWEEN ? AND ?", (s.isoformat(), e.isoformat())):
                agg[dt.date.fromisoformat(r["date"]).weekday()] += (r["amount"] or 0) if metric != "transactions" else 1
            series = [{"label": str(i), "value": v} for i, v in enumerate(agg)]
            lf = "weekday"
        else:
            series = [{"label": k, "value": v} for k, v in cur["expenses_by_category"].items()]
            lf, fmt = "text", "money"
        return block("chart", title=title or T(f"chart.by_{by}"), kind=b.get("kind", "bar"), fmt=fmt, label_fmt=lf, series=series)

    def _source_table(self, source: str, limit: int, title) -> Optional[dict]:
        if source.startswith("segment:"):
            v = self.find_customers(source.split(":", 1)[1], limit=limit)
            t = next(b for b in v["blocks"] if b["type"] == "table")
            t["title"] = title or v["title"]
            return t
        if source == "offerings":
            b = self._offerings_block()
        elif source == "staff":
            b = self._staff_block(M.period_summary(self.conn, *M.month_bounds(M.last_full_month(self.today))))
        elif source == "decisions":
            b = self._decisions_block()
        elif source == "cohorts":
            rows = [{"month": x["month"], "new": x["new"], "returned": x["returned"], "rate": x["return_rate_pct"], "offering": x["top_first_offering"]}
                    for x in M.cohorts(self.conn, self.today)][-limit:]
            b = block("table", title=T("sec.cohorts"), columns=[col("month"), col("new", "number"), col("returned", "number"), col("rate", "percent"), col("offering", "text", "col.first_offering")], rows=rows)
        else:
            costs = M.costs_for(self.conn, *M.month_bounds(M.last_full_month(self.today)))
            b = block("table", title=T("sec.costs"), columns=[col("name"), col("amount", "money")],
                      rows=[{"name": k, "amount": v} for k, v in costs["by_category"].items()])
        if b and title:
            b["title"] = title
        return b

    # -------------------------------------------------------------- decisions
    def prepare_decision_context(self, topic: str, offering: Optional[str] = None) -> dict:
        c = self.conn
        lm = M.last_full_month(self.today)
        cur = M.period_summary(c, *M.month_bounds(lm))
        three = M.period_summary(c, M.month_bounds(M.shift_month(lm, -2))[0], M.month_bounds(lm)[1])
        margins = M.offering_margins(c)
        key = fold(offering or topic)
        target = next((m for m in margins if fold(m["name"]) in key or (offering and key in fold(m["name"]))), None)
        blocks = []
        if target and target["margin"] is not None:
            o = next((x for x in three["by_offering"] if x["name"] == target["name"]), {"revenue": 0, "count": 0, "customers": 0})
            rows = []
            for disc in (0, 10, 15, 20, 30):
                price = round(target["price"] * (1 - disc / 100))
                margin = price - target["unit_cost"]
                rows.append({"discount": disc, "price": price, "margin": margin, "needed": round(target["margin"] / margin, 2) if margin > 0 else None})
            blocks.append(block("table", title=T("sec.unit_economics", name=target["name"]),
                                columns=[col("discount", "percent"), col("price", "money"), col("margin", "money"), col("needed", "number")], rows=rows))
            blocks.append(block("kv", items=[{"t": "kv.last_3m_count", "v": o["count"], "fmt": "number"},
                                             {"t": "kv.last_3m_revenue", "v": o["revenue"], "fmt": "money"},
                                             {"t": "kv.last_3m_customers", "v": o["customers"], "fmt": "number"}]))
        coh = M.cohorts(c, self.today)
        if coh:
            blocks.append(block("table", title=T("sec.cohorts"), columns=[col("month"), col("new", "number"), col("returned", "number"), col("rate", "percent"), col("offering", "text", "col.first_offering")],
                                rows=[{"month": x["month"], "new": x["new"], "returned": x["returned"], "rate": x["return_rate_pct"],
                                       "offering": x["top_first_offering"]} for x in coh]))
        lapsed = M.segment(M.customer_stats(c, self.today), "lapsed_regulars")
        blocks.append(block("kv", title=T("sec.now"), items=[{"t": "kpi.revenue", "v": cur["revenue"], "fmt": "money"},
                                                              {"t": "kpi.net_profit", "v": cur["net_profit"], "fmt": "money"},
                                                              {"t": "kv.lapsed", "v": len(lapsed), "fmt": "number"}]))
        notes = self._search(topic, 4)
        goals = c.execute("SELECT section, text FROM knowledge WHERE kind='goal' OR lower(section) LIKE '%obiectiv%' OR lower(section) LIKE '%objecti%' "
                          "OR lower(section) LIKE '%ziel%' OR lower(section) LIKE '%goal%'").fetchall()
        seen = {n["text"] for n in notes}
        items = [{"kind": None, "title": n["section"], "text": n["text"]} for n in notes] + \
                [{"kind": "goal", "title": g["section"], "text": g["text"]} for g in goals if g["text"] not in seen]
        if items:
            blocks.append(block("notes", title=T("sec.related_notes"), items=items))
        if (d := self._decisions_block()):
            blocks.append(d)
        return view("prepare_decision_context", T("view.decision", topic=topic), blocks,
                    hints=["'needed' = multiple of the current volume required to keep the same profit at that discount.",
                           "Give a clear recommendation with numbers; if the owner decides, capture it (type=decision, with a review date)."])

    # -------------------------------------------------------------- knowledge
    def _search(self, query: str, limit: int = 5) -> list[sqlite3.Row]:
        stems = {w[:5] for w in re.split(r"\W+", fold(query)) if len(w) >= 3}
        scored = []
        for r in self.conn.execute("SELECT section, text, kind FROM knowledge"):
            hay = fold(r["section"] + " " + r["text"])
            score = sum(hay.count(s) for s in stems)
            if score:
                scored.append((score, r))
        return [r for _, r in sorted(scored, key=lambda x: -x[0])[:limit]]

    def search_knowledge(self, query: str) -> str:
        hits = self._search(query)
        people = self._search_facts(query)
        if people:
            head = "## What people said\n" + "\n".join(people)
            return head + ("\n\n" + "\n\n".join(f"## {h['section']}\n{h['text']}" for h in hits) if hits else "")
        if not hits:
            return "No note matches. Sections: " + ", ".join(r["section"] for r in self.conn.execute("SELECT DISTINCT section FROM knowledge"))
        return "\n\n".join(f"## {h['section']}\n{h['text']}" for h in hits)

    def _search_facts(self, query: str, limit: int = 12) -> list[str]:
        from .people import active_clause, fact_kinds
        stems = {w[:5] for w in re.split(r"\W+", fold(query)) if len(w) >= 3}
        kinds = fact_kinds(self.conn)
        scored = []
        for r in self.conn.execute(f"SELECT f.*, p.name FROM facts f JOIN v_parties p ON p.id=f.record_id WHERE {active_clause(self.today, 'f')} "
                                   "AND f.sensitivity != 'special'"):
            label = kinds.get(r["kind"], {"label": {"en": r["kind"]}})["label"]["en"]
            hay = fold(f"{r['name']} {label} {r['kind']} {r['key'] or ''} {r['value']} {r['polarity']}")
            score = sum(hay.count(s) for s in stems)
            if score:
                scored.append((score, f"- {r['name']} · {label}{' (' + r['key'] + ')' if r['key'] else ''}: {r['value']} "
                                      f"[{r['polarity']}, said {r['observed_at']}, {r['source']}" + (f', “{r["quote"]}”' if r["quote"] else "") + "]"))
        return [t for _, t in sorted(scored, key=lambda x: -x[0])[:limit]]

    def query_data(self, sql: str) -> str:
        if not re.match(r"^\s*(select|with)\b", sql, re.I):
            return "Error: only SELECT queries are allowed."
        try:
            cur = self.readonly().execute(sql)
            rows = cur.fetchmany(QUERY_MAX_ROWS)
            more = len(cur.fetchmany(1000))
        except Exception as exc:  # noqa: BLE001
            return (f"Error: {exc}. Views: v_transactions(entity, date, time, party_id, party, phone, offering, category, staff, amount, "
                    "quantity, status, counted, unit_cost, source), v_customers(id, name, phone, visits, total_spend, first_seen, last_visit), "
                    "v_offerings(id, name, price, unit_cost, category), v_staff(id, name), v_costs(month, category, amount), "
                    "v_facts(person_id, person, kind, key, value, polarity, sensitivity, confidence, observed_at, current), "
                    "v_relations(type, a_id, a_name, b_id, b_name). "
                    f"Money is in minor units (cents) of {self.currency}. Tables: " + ", ".join(schema.table(n) for n in schema.entities(self.conn)))
        if not rows:
            return "(no rows)"
        keys = list(rows[0].keys())

        def cell(x) -> str:
            s = "-" if x is None else str(x).replace("\n", " ").replace("|", "/")
            return s if len(s) <= 120 else s[:117] + "..."
        out = ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)] + ["| " + " | ".join(cell(x) for x in r) + " |" for r in rows]
        if more:
            out.append(f"(+{more}{'+' if more >= 1000 else ''} more rows not shown: select only the columns you need, "
                       "filter with WHERE, aggregate with COUNT/SUM/GROUP BY, or add LIMIT)")
        return "\n".join(out)

    # ------------------------------------------------------------ report data
    def get_report_data(self, kind: str = "monthly", period: str = "last_month") -> str:
        """Exact data + design tokens for building an Artifact (report, dashboard, poster)."""
        s, e = _period(period, self.today)
        cur = M.period_summary(self.conn, s, e)
        prev = M.period_summary(self.conn, *_previous(period, s, e))
        stats = M.customer_stats(self.conn, self.today)
        spec = design.get_spec(self.conn)
        data = {
            "business": {"name": self.setting("name"), "vertical": self.setting("vertical"), "locale": self.lang, "currency": self.currency},
            "period": {"start": s.isoformat(), "end": e.isoformat()},
            "money_unit": "minor units: divide by 100",
            "kpis": {k: {"value": cur[k], "previous": prev[k]} for k in ("revenue", "transactions", "receipts", "unique_customers", "new_customers",
                                                                         "avg_ticket", "cost_of_goods", "gross_profit", "expenses", "net_profit", "no_show_rate")},
            "by_offering": cur["by_offering"], "by_staff": cur["by_staff"], "by_hour": cur["by_hour"], "costs": cur["expenses_by_category"],
            "trend": M.monthly_trend(self.conn, f"{e.year:04d}-{e.month:02d}", 12),
            "offerings": M.offering_margins(self.conn),
            "segments": {seg: len(M.segment(stats, seg)) for seg in M.SEGMENTS},
            "top_lapsed": [{"name": x.name, "spend": x.total_spend, "last_visit": x.last_visit} for x in M.segment(stats, "lapsed_regulars")[:10]],
            "labels": {r: {"singular": self.label(r, False, self.lang), "plural": self.label(r, True, self.lang)} for r in ("party", "staff", "offering", "transaction")},
            "design": {"brand": {k: v for k, v in spec["brand"].items() if k != "logo"}, "has_logo": bool(spec["brand"].get("logo")),
                       "in_the_know": {"ink": "#14161F", "signal": "#4F46E5", "mint": "#2BD9A8", "font": "Inter"}},
        }
        if kind == "customers":
            data["customers"] = [s_.as_dict() for s_ in sorted(stats, key=lambda x: -x.total_spend)[:100]]
        return ("Build the Artifact from this exact data; never change numbers. Format money in the business currency and locale, "
                "write in the owner's language, use the brand colors. Footer: 'Made with In The Know'.\n```json\n"
                + json.dumps(data, ensure_ascii=False, default=str) + "\n```")

    # ------------------------------------------------------------- dispatcher
    def _mark_used(self, what: str) -> None:
        self._log_use(f"used:{what}")

    def _log_use(self, tool: str) -> None:
        # Only the set of tools ever used matters (onboarding checklist), so repeat uses write
        # nothing and a read-only call never makes the memory file "changed".
        self.conn.execute("INSERT INTO usage_log(tool) SELECT ? WHERE NOT EXISTS (SELECT 1 FROM usage_log WHERE tool=?)",
                          (tool, tool))

    def _run_once(self, fn: Callable, name: str, args: dict):
        self.conn.hold = True
        try:
            result = fn(**{k: v for k, v in args.items() if v is not None})
            self._log_use(name)
        except Exception:
            self.conn.hold = False
            self.conn.rollback()                # a failed tool never leaves a half-applied change
            self._mark_clean()
            raise
        self.conn.hold = False
        self.conn.commit()
        return result

    def tool_functions(self) -> dict[str, Callable]:
        return {name: getattr(self, name) for name in (
            "get_started", "setup_business", "get_context", "get_metrics", "complete_task", "get_business_brief", "get_action_list", "review_period", "find_customers",
            "get_person_brief", "get_customer_context", "get_data_status", "show_view", "prepare_decision_context", "search_knowledge", "query_data",
            "get_report_data", "add_data", "import_records", "map_columns", "capture", "change_schema", "update_design",
            "define_metric", "merge_customers", "resolve_question", "mark_contacted", "undo_change", "import_chat_history", "import_uploads", "upload_card",
            "edit_records", "browse_records", "find_records")}

    def call(self, name: str, args: dict) -> str:
        return self.call_view(name, args)[0]

    def call_view(self, name: str, args: dict) -> tuple[str, Optional[dict]]:
        fn = self.tool_functions().get(name)
        if fn is None:
            raise KeyError(name)
        with self._lock:
            self._sync()
            for attempt in range(2):
                result = self._run_once(fn, name, args)
                if not self._dirty() or (self._version is None and not self.is_setup):
                    break                       # no file is created in the owner's storage before setup
                try:
                    self._persist()
                    break
                except Conflict:                # another device saved first: take its version, redo once
                    self._reload()
                    if attempt:
                        raise
                except Exception:               # nothing half-saved stays in memory
                    self._reload()
                    raise
            if isinstance(result, str):
                return result, None
            return to_text(result, self.currency), self.envelope(result)

    def envelope(self, v: dict) -> dict:
        spec = design.get_spec(self.conn)
        return {"v": 2, "view": v["view"], "title": v["title"], "subtitle": v.get("subtitle"), "blocks": v["blocks"],
                "lang": self.lang, "currency": self.currency, "business": self.setting("name") or "",
                "vertical": self.setting("vertical") or "", "brand": dict(spec["brand"]), "today": self.today.isoformat(),
                "labels": {r: {lg: [schema.label(s, lg, False), schema.label(s, lg, True)] for lg in schema.LANGS}
                           for r in ("party", "staff", "offering", "transaction") if (s := schema.primary(self.conn, r))},
                "entity_labels": {n: {lg: schema.label(s, lg, False) for lg in schema.LANGS} for n, s in schema.entities(self.conn).items()}}
