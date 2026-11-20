"""Metric layer: every business number is defined once, here, over the role views.

Works for any vertical because it only reads v_transactions, v_parties, v_offerings,
v_staff and v_costs. Custom metrics defined in chat are validated read-only SELECTs.
"""
from __future__ import annotations

import calendar
import datetime as dt
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Optional

from . import schema

NO_SHOW = "no_show"


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def month_bounds(month: str) -> tuple[dt.date, dt.date]:
    y, m = map(int, month.split("-"))
    return dt.date(y, m, 1), dt.date(y, m, calendar.monthrange(y, m)[1])


def last_full_month(today: dt.date) -> str:
    prev = today.replace(day=1) - dt.timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def shift_month(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def pct_change(new: float, old: float) -> Optional[float]:
    """Percent change; None when the base is zero or negative (a % of a loss is misleading)."""
    return None if not old or old < 0 else round((new - old) / old * 100, 1)


def receipts_expr(conn: sqlite3.Connection) -> Optional[str]:
    """For shops that log one row per item, a 'receipt' groups rows into one purchase."""
    for tx in schema.by_role(conn, "transaction"):
        if schema.field(tx, "receipt"):
            return tx["name"]
    return None


# --- Periods -------------------------------------------------------------------

def period_summary(conn: sqlite3.Connection, start: dt.date, end: dt.date) -> dict:
    s, e = start.isoformat(), end.isoformat()
    rows = conn.execute("SELECT * FROM v_transactions WHERE date BETWEEN ? AND ?", (s, e)).fetchall()
    counted = [r for r in rows if r["counted"]]
    first_seen = {r["id"]: r["first_seen"] for r in conn.execute("SELECT id, first_seen FROM v_customers")}
    revenue = sum(r["amount"] or 0 for r in counted)
    cost_of_goods = sum(r["unit_cost"] or 0 for r in counted)
    parties = {r["party_id"] for r in counted if r["party_id"] is not None}
    new = {p for p in parties if first_seen.get(p) and s <= first_seen[p] <= e}
    with_status = [r for r in rows if r["status"]]
    no_shows = sum(1 for r in with_status if r["status"] == NO_SHOW)

    rec_entity = receipts_expr(conn)
    if rec_entity:
        tbl = schema.table(rec_entity)
        receipts = conn.execute(f"SELECT COUNT(DISTINCT date || '#' || COALESCE(receipt, id)) FROM {tbl} WHERE date BETWEEN ? AND ?",
                                (s, e)).fetchone()[0]
    else:
        receipts = len(counted)

    def breakdown(key: str) -> list[dict]:
        agg: dict[str, dict] = defaultdict(lambda: {"revenue": 0, "count": 0, "quantity": 0.0, "parties": set()})
        for r in counted:
            k = r[key] or "—"
            agg[k]["revenue"] += r["amount"] or 0
            agg[k]["count"] += 1
            agg[k]["quantity"] += r["quantity"] or 1
            if r["party_id"] is not None:
                agg[k]["parties"].add(r["party_id"])
        out = [{"name": k, "revenue": v["revenue"], "count": v["count"], "quantity": round(v["quantity"], 1),
                "customers": len(v["parties"])} for k, v in agg.items()]
        return sorted(out, key=lambda x: -x["revenue"])

    hours = Counter()
    for r in counted:
        if r["time"]:
            hours[int(r["time"][:2])] += r["amount"] or 0
    costs = costs_for(conn, start, end)
    return {
        "start": s, "end": e,
        "revenue": revenue,
        "transactions": len(counted),
        "receipts": receipts,
        "items_sold": round(sum(r["quantity"] or 1 for r in counted), 1),
        "unique_customers": len(parties),
        "new_customers": len(new),
        "avg_ticket": round(revenue / receipts) if receipts else 0,
        "cost_of_goods": cost_of_goods,
        "gross_profit": revenue - cost_of_goods,
        "expenses": costs["total"],
        "expenses_by_category": costs["by_category"],
        "expenses_complete": costs["complete"],
        "net_profit": revenue - cost_of_goods - costs["total"],
        "unit_cost_missing": sum(1 for r in counted if r["unit_cost_missing"]),
        "no_shows": no_shows,
        "no_show_rate": round(no_shows / len(with_status) * 100, 1) if with_status else None,
        "by_offering": breakdown("offering"),
        "by_staff": breakdown("staff"),
        "by_hour": dict(sorted(hours.items())),
    }


def costs_for(conn: sqlite3.Connection, start: dt.date, end: dt.date) -> dict:
    """Monthly costs prorated by the share of each month that falls in [start, end]."""
    total, by_cat, months_seen = 0.0, Counter(), set()
    explicit = {(r["month"], (r["category"] or "").strip().lower()) for r in conn.execute("SELECT month, category FROM v_costs")}
    rows = [dict(r) for r in conn.execute("SELECT month, category, amount FROM v_costs")]
    for rc in conn.execute("SELECT category, amount, start_month, end_month FROM recurring_costs"):
        m = rc["start_month"]
        last = f"{end.year:04d}-{end.month:02d}"
        while m <= last and (rc["end_month"] is None or m <= rc["end_month"]):
            if (m, rc["category"].strip().lower()) not in explicit:      # an actual recorded cost wins over the recurring one
                rows.append({"month": m, "category": rc["category"], "amount": rc["amount"]})
            m = shift_month(m, 1)
    for r in rows:
        if not r["month"]:
            continue
        m_start, m_end = month_bounds(r["month"])
        overlap = (min(end, m_end) - max(start, m_start)).days + 1
        if overlap <= 0:
            continue
        share = overlap / ((m_end - m_start).days + 1)
        total += (r["amount"] or 0) * share
        by_cat[r["category"]] += (r["amount"] or 0) * share
        months_seen.add(r["month"])
    needed, cur = set(), start.replace(day=1)
    while cur <= end:
        needed.add(f"{cur.year:04d}-{cur.month:02d}")
        cur = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return {"total": round(total), "by_category": {k: round(v) for k, v in by_cat.most_common()}, "complete": needed <= months_seen}


def change_drivers(cur: dict, prev: dict, key: str) -> list[dict]:
    a = {x["name"]: x for x in cur[key]}
    b = {x["name"]: x for x in prev[key]}
    out = [{"name": n, "current": a.get(n, {}).get("revenue", 0), "previous": b.get(n, {}).get("revenue", 0)} for n in set(a) | set(b)]
    for x in out:
        x["delta"] = x["current"] - x["previous"]
    return sorted(out, key=lambda x: x["delta"])


def monthly_trend(conn: sqlite3.Connection, end_month: str, months: int = 6) -> list[dict]:
    out = []
    for i in range(months - 1, -1, -1):
        m = shift_month(end_month, -i)
        s, e = month_bounds(m)
        r = conn.execute("SELECT COALESCE(SUM(amount),0) rev, COUNT(*) n FROM v_transactions WHERE counted=1 AND date BETWEEN ? AND ?",
                         (s.isoformat(), e.isoformat())).fetchone()
        out.append({"month": m, "revenue": r["rev"], "count": r["n"]})
    return out


# --- Customers ---------------------------------------------------------------------

@dataclass
class CustomerStat:
    id: int
    name: str
    phone: Optional[str]
    visits: int
    total_spend: int
    first_seen: Optional[str]
    last_visit: Optional[str]
    days_since_last: Optional[int]
    avg_interval_days: Optional[float]
    favorite_offering: Optional[str]
    favorite_staff: Optional[str]
    last_staff: Optional[str]
    no_shows: int
    status: str
    last_contacted: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


def customer_stats(conn: sqlite3.Connection, today: dt.date) -> list[CustomerStat]:
    visits, noshow = defaultdict(list), Counter()
    for r in conn.execute("SELECT party_id, date, offering, staff, amount, counted, status FROM v_transactions WHERE party_id IS NOT NULL ORDER BY date, time"):
        if r["counted"]:
            visits[r["party_id"]].append(r)
        elif r["status"] == NO_SHOW:
            noshow[r["party_id"]] += 1
    contacted = {r["party_id"]: r["d"] for r in conn.execute("SELECT party_id, MAX(contacted_on) d FROM contact_log GROUP BY party_id")}
    stats = []
    for c in conn.execute("SELECT * FROM v_customers"):
        vs = visits.get(c["id"], [])
        dates = sorted({d(v["date"]) for v in vs})
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        avg = round(sum(gaps) / len(gaps), 1) if gaps else None
        days_since = (today - dates[-1]).days if dates else None
        fav_o = Counter(v["offering"] for v in vs if v["offering"]).most_common(1)
        fav_s = Counter(v["staff"] for v in vs if v["staff"]).most_common(1)
        stats.append(CustomerStat(
            id=c["id"], name=c["name"], phone=c["phone"], visits=len(dates), total_spend=sum(v["amount"] or 0 for v in vs),
            first_seen=c["first_seen"], last_visit=dates[-1].isoformat() if dates else None, days_since_last=days_since,
            avg_interval_days=avg, favorite_offering=fav_o[0][0] if fav_o else None,
            favorite_staff=fav_s[0][0] if fav_s else None, last_staff=vs[-1]["staff"] if vs else None,
            no_shows=noshow[c["id"]], status=_status(len(dates), days_since, avg), last_contacted=contacted.get(c["id"])))
    return stats


def _status(visits: int, days_since: Optional[int], avg: Optional[float]) -> str:
    if days_since is None:
        return "no_visits"
    if visits >= 3 and avg:
        if days_since > max(60, 2 * avg):
            return "lapsed_regular"
        if days_since > 1.5 * avg:
            return "at_risk"
        return "active_regular"
    if days_since > 90:
        return "inactive"
    return "active"


SEGMENTS = ("due_soon", "lapsed_regulars", "at_risk", "top_spenders", "active", "one_time")


def segment(stats: list[CustomerStat], name: str, min_visits: int = 3, min_days_since: int = 60) -> list[CustomerStat]:
    if name == "lapsed_regulars":
        out = [s for s in stats if s.visits >= min_visits and (s.days_since_last or 0) >= min_days_since]
    elif name == "at_risk":
        out = [s for s in stats if s.status == "at_risk"]
    elif name == "top_spenders":
        out = [s for s in stats if s.visits]
    elif name == "active":
        out = [s for s in stats if s.days_since_last is not None and s.days_since_last <= 90]
    elif name == "one_time":
        out = [s for s in stats if s.visits == 1]
    elif name == "due_soon":
        out = [s for s in stats if s.status == "active_regular" and s.avg_interval_days and s.days_since_last >= s.avg_interval_days - 7]
        return sorted(out, key=lambda s: s.avg_interval_days - s.days_since_last)
    else:
        raise ValueError(f"unknown segment {name}; use one of {SEGMENTS}")
    return sorted(out, key=lambda s: -s.total_spend)


def staff_activity(conn: sqlite3.Connection, today: dt.date) -> list[dict]:
    rows = conn.execute("SELECT staff, MIN(date) first, MAX(date) last, COUNT(*) n FROM v_transactions WHERE staff IS NOT NULL GROUP BY staff").fetchall()
    return [{"name": r["staff"], "first": r["first"], "last": r["last"], "count": r["n"], "active": (today - d(r["last"])).days <= 21} for r in rows]


def lapsed_by_staff(stats: list[CustomerStat], min_visits: int = 3, min_days_since: int = 60) -> Counter:
    return Counter(s.last_staff or "—" for s in segment(stats, "lapsed_regulars", min_visits, min_days_since))


# --- Offerings & cohorts --------------------------------------------------------------

def offering_margins(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT name, price, unit_cost FROM v_offerings"):
        p, c = r["price"], r["unit_cost"]
        margin = p - c if p is not None and c is not None else None
        out.append({"name": r["name"], "price": p, "unit_cost": c, "margin": margin,
                    "margin_pct": round(margin / p * 100, 1) if margin is not None and p else None})
    return sorted(out, key=lambda x: -(x["margin_pct"] if x["margin_pct"] is not None else -1))


def cohorts(conn: sqlite3.Connection, today: dt.date, window_days: int = 120) -> list[dict]:
    """For customers whose first visit falls in each month: how many came back later."""
    first, first_off, later = {}, {}, Counter()
    for r in conn.execute("SELECT party_id, date, offering FROM v_transactions WHERE counted=1 AND party_id IS NOT NULL ORDER BY date"):
        pid = r["party_id"]
        if pid not in first:
            first[pid], first_off[pid] = r["date"], r["offering"]
        elif r["date"] > first[pid]:
            later[pid] += 1
    known_before = {r["id"] for r in conn.execute("SELECT id, first_seen, first_visit_in_data FROM v_customers")
                    if r["first_seen"] and r["first_visit_in_data"] and r["first_seen"] < r["first_visit_in_data"]}
    by_month: dict[str, dict] = defaultdict(lambda: {"new": 0, "returned": 0, "offerings": Counter()})
    for pid, date in first.items():
        if pid in known_before:
            continue
        b = by_month[date[:7]]
        b["new"] += 1
        b["offerings"][first_off[pid]] += 1
        b["returned"] += 1 if later[pid] else 0
    return [{"month": m, "new": v["new"], "returned": v["returned"],
             "return_rate_pct": round(v["returned"] / v["new"] * 100) if v["new"] else None,
             "top_first_offering": v["offerings"].most_common(1)[0][0] if v["offerings"] else None,
             "mature": (today - d(f"{m}-01")).days >= window_days} for m, v in sorted(by_month.items())]


# --- Custom metrics defined in chat -----------------------------------------------------

_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|pragma|replace|vacuum)\b", re.I)


def validate_metric_sql(sql: str) -> str:
    sql = sql.strip().rstrip(";")
    if not re.match(r"^(select|with)\b", sql, re.I) or _FORBIDDEN.search(sql) or ";" in sql:
        raise ValueError("metric must be a single read-only SELECT")
    return sql


def eval_metric(ro_conn: sqlite3.Connection, sql: str, start: dt.date, end: dt.date) -> Optional[float]:
    row = ro_conn.execute(sql, {"start": start.isoformat(), "end": end.isoformat()}).fetchone()
    if row is None:
        return None
    v = row[0]
    return float(v) if v is not None else None


def custom_metrics(conn: sqlite3.Connection) -> list[dict]:
    return [{"name": r["name"], "label": json.loads(r["label"]), "sql": r["sql"], "format": r["format"]}
            for r in conn.execute("SELECT * FROM metric_defs ORDER BY name")]


def data_quality(conn: sqlite3.Connection) -> list[dict]:
    issues = []
    for r in conn.execute("SELECT kind, payload FROM questions WHERE status='open'"):
        n = json.loads(r["payload"]).get("count", 1)
        kind = "new_party" if r["kind"] == "new_parties" else r["kind"]
        hit = next((i for i in issues if i["kind"] == kind), None)
        if hit:
            hit["count"] += n
        else:
            issues.append({"kind": kind, "count": n})
    missing = conn.execute("SELECT COUNT(*) FROM v_offerings WHERE unit_cost IS NULL").fetchone()[0]
    if missing:
        issues.append({"kind": "offering_cost_missing", "count": missing})
    return issues
