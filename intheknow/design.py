"""Design spec: brand tokens + layouts + custom views, editable from chat.

The assistant never writes HTML. It sends a patch; the patch is validated against a closed
vocabulary (colors, fonts, block types, metrics, sources) and stored as a new version that
can be undone. The renderer turns the spec into cards.
"""
from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from typing import Any, Optional

from .db import get_setting, set_setting

FONTS = ["Inter", "Manrope", "DM Sans", "Nunito", "Space Grotesk", "Fraunces", "Playfair Display", "IBM Plex Sans"]
RADIUS = {"none": 0, "sm": 6, "md": 12, "lg": 18, "xl": 26}
DENSITY = ("compact", "comfortable", "airy")
MODES = ("auto", "light", "dark")
BRIEF_SECTIONS = ["kpis", "attention", "memory", "trend", "hours", "staff", "offerings", "decisions", "notes", "quality"]
KPI_METRICS = ["revenue", "transactions", "receipts", "unique_customers", "new_customers", "avg_ticket", "cost_of_goods",
               "gross_profit", "expenses", "net_profit", "no_show_rate", "items_sold"]
PERIODS = ("last_month", "this_month", "last_30d", "last_90d", "last_12m")
CHART_BY = ("month", "offering", "staff", "hour", "weekday", "category")
TABLE_SOURCES = ("segment:due_soon", "segment:lapsed_regulars", "segment:at_risk", "segment:top_spenders",
                 "segment:active", "segment:one_time", "offerings", "staff", "costs", "cohorts", "decisions")
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_VIEW = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
MAX_VIEWS = 12
MAX_BLOCKS = 10
MAX_LOGO = 200_000


def default_spec(pack: dict) -> dict:
    return {
        "brand": {"primary": pack["theme"]["primary"], "accent": pack["theme"]["accent"], "font": "Inter",
                  "radius": "md", "density": "comfortable", "mode": "auto", "logo": None, "show_brand": True},
        "brief": {"sections": list(pack.get("brief_layout") or BRIEF_SECTIONS), "kpis": list(pack["kpis"])},
        "views": {},
    }


def get_spec(conn: sqlite3.Connection) -> dict:
    raw = get_setting(conn, "design")
    if raw:
        return json.loads(raw)
    from .verticals import pack
    return default_spec(pack(get_setting(conn, "vertical", "generic")))


def _err(msg: str):
    raise ValueError(msg)


def _validate_block(b: dict, custom_metrics: set[str]) -> dict:
    t = b.get("type")
    title = b.get("title")
    if title is not None and (not isinstance(title, (str, dict)) or len(json.dumps(title)) > 300):
        _err("title must be a short string or {lang: text}")
    period = b.get("period", "last_month")
    if period not in PERIODS:
        _err(f"period must be one of {PERIODS}")
    out: dict[str, Any] = {"type": t, "period": period}
    if title is not None:
        out["title"] = title
    if t == "kpis":
        ms = b.get("metrics") or []
        bad = [m for m in ms if m not in KPI_METRICS and m not in custom_metrics]
        if not ms or len(ms) > 6 or bad:
            _err(f"kpis.metrics: 1-6 of {KPI_METRICS + sorted(custom_metrics)} (bad: {bad})")
        out["metrics"] = ms
    elif t == "chart":
        if b.get("metric", "revenue") not in ("revenue", "transactions", "net_profit") or b.get("by") not in CHART_BY:
            _err(f"chart: metric revenue|transactions|net_profit, by one of {CHART_BY}")
        out.update(metric=b.get("metric", "revenue"), by=b["by"], kind=b.get("kind", "bar") if b.get("kind") in ("bar", "line") else "bar")
    elif t == "table":
        if b.get("source") not in TABLE_SOURCES:
            _err(f"table.source must be one of {TABLE_SOURCES}")
        out.update(source=b["source"], limit=max(1, min(int(b.get("limit", 10)), 50)))
    elif t == "alerts":
        pass
    else:
        _err("block type must be kpis | chart | table | alerts")
    return out


def apply_patch(conn: sqlite3.Connection, patch: dict, note: Optional[str] = None) -> tuple[dict, list[str]]:
    """Validate and apply a design patch. Returns (new spec, list of human-readable changes)."""
    spec = get_spec(conn)
    new = deepcopy(spec)
    changes: list[str] = []
    customs = {r["name"] for r in conn.execute("SELECT name FROM metric_defs")}

    brand = patch.get("brand") or {}
    for k, v in brand.items():
        if k in ("primary", "accent"):
            if not isinstance(v, str) or not _HEX.match(v):
                _err(f"brand.{k} must be a hex color like #2F6FDE")
        elif k == "font" and v not in FONTS:
            _err(f"brand.font must be one of {FONTS}")
        elif k == "radius" and v not in RADIUS:
            _err(f"brand.radius must be one of {list(RADIUS)}")
        elif k == "density" and v not in DENSITY:
            _err(f"brand.density must be one of {DENSITY}")
        elif k == "mode" and v not in MODES:
            _err(f"brand.mode must be one of {MODES}")
        elif k == "logo":
            if v is not None and (not isinstance(v, str) or not re.match(r"^data:image/(png|jpeg|svg\+xml|webp);base64,", v) or len(v) > MAX_LOGO):
                _err("brand.logo must be a data:image/(png|jpeg|svg+xml|webp);base64 URI under 200 KB, or null")
        elif k == "show_brand" and not isinstance(v, bool):
            _err("brand.show_brand must be true/false")
        elif k not in ("primary", "accent", "font", "radius", "density", "mode", "logo", "show_brand"):
            _err(f"unknown brand key {k}")
        new["brand"][k] = v
        changes.append(f"brand.{k}")

    brief = patch.get("brief") or {}
    if "sections" in brief:
        secs = brief["sections"]
        bad = [s for s in secs if s not in BRIEF_SECTIONS]
        if bad or not secs or len(set(secs)) != len(secs):
            _err(f"brief.sections: ordered unique list from {BRIEF_SECTIONS} (bad: {bad})")
        new["brief"]["sections"] = secs
        changes.append("brief.sections")
    if "kpis" in brief:
        ms = brief["kpis"]
        bad = [m for m in ms if m not in KPI_METRICS and m not in customs]
        if bad or not ms or len(ms) > 6:
            _err(f"brief.kpis: 1-6 of {KPI_METRICS + sorted(customs)} (bad: {bad})")
        new["brief"]["kpis"] = ms
        changes.append("brief.kpis")

    for name, view in (patch.get("views") or {}).items():
        if not _VIEW.match(name):
            _err("view names: lowercase letters, digits, underscore")
        if view is None:
            new["views"].pop(name, None)
            changes.append(f"views.{name} removed")
            continue
        blocks = view.get("blocks") or []
        if not blocks or len(blocks) > MAX_BLOCKS:
            _err(f"a view needs 1-{MAX_BLOCKS} blocks")
        title = view.get("title") or name.replace("_", " ").title()
        new["views"][name] = {"title": title, "blocks": [_validate_block(b, customs) for b in blocks]}
        changes.append(f"views.{name}")
    if len(new["views"]) > MAX_VIEWS:
        _err(f"max {MAX_VIEWS} custom views")
    if not changes:
        _err("empty patch: send brand, brief or views")

    conn.execute("INSERT INTO design_history(spec, note) VALUES (?,?)", (json.dumps(spec, ensure_ascii=False), note))
    set_setting(conn, "design", json.dumps(new, ensure_ascii=False))
    conn.commit()
    return new, changes


def undo(conn: sqlite3.Connection) -> Optional[dict]:
    r = conn.execute("SELECT id, spec FROM design_history ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return None
    set_setting(conn, "design", r["spec"])
    conn.execute("DELETE FROM design_history WHERE id=?", (r["id"],))
    conn.commit()
    return json.loads(r["spec"])


def capabilities() -> dict:
    """What can be designed (sent to the assistant so it can propose valid patches)."""
    return {"brand": {"primary": "#RRGGBB", "accent": "#RRGGBB", "font": FONTS, "radius": list(RADIUS), "density": list(DENSITY),
                      "mode": list(MODES), "logo": "data:image/...;base64 (<200KB) or null", "show_brand": "bool"},
            "brief": {"sections": BRIEF_SECTIONS, "kpis": KPI_METRICS + ["<custom metric name>"]},
            "views": {"<name>": {"title": "text or {en,fr,ro,de}", "blocks": [
                {"type": "kpis", "metrics": "1-6 metrics", "period": list(PERIODS)},
                {"type": "chart", "metric": "revenue|transactions|net_profit", "by": list(CHART_BY), "kind": "bar|line", "period": list(PERIODS)},
                {"type": "table", "source": list(TABLE_SOURCES), "limit": "1-50"},
                {"type": "alerts"}]}}}
