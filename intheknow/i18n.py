"""Translations (en, fr, ro, de) and locale formatting.

View models carry i18n keys + typed params; the UI renders them in the owner's language,
and the model-facing text is rendered in English from the same keys.
Typed params: {"$m": minor_units} money, {"$n": n} number, {"$p": pct} percent, {"$d": "YYYY-MM-DD"} date,
{"$mo": "YYYY-MM"} month, {"$k": "key"} nested translation.
"""
from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from pathlib import Path

LANGS = ("en", "fr", "ro", "de")
DIR = Path(__file__).parent / "ui" / "i18n"
SEP = {"en": (",", "."), "fr": (" ", ","), "ro": (".", ","), "de": (".", ",")}
CUR = {"EUR": "€", "RON": "lei", "USD": "$", "GBP": "£", "CHF": "CHF"}
MONTHS = {
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "fr": ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."],
    "ro": ["ian.", "feb.", "mar.", "apr.", "mai", "iun.", "iul.", "aug.", "sept.", "oct.", "nov.", "dec."],
    "de": ["Jan.", "Feb.", "März", "Apr.", "Mai", "Juni", "Juli", "Aug.", "Sept.", "Okt.", "Nov.", "Dez."],
}


@lru_cache(maxsize=None)
def table(lang: str) -> dict[str, str]:
    path = DIR / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def all_tables() -> dict[str, dict[str, str]]:
    return {lang: table(lang) for lang in LANGS}


def num(x: float, lang: str = "en", decimals: int = 0) -> str:
    thou, dec = SEP.get(lang, SEP["en"])
    s = f"{abs(x):,.{decimals}f}".replace(",", "\0").replace(".", dec).replace("\0", thou)
    return ("-" if x < 0 else "") + s


def money(minor, currency: str = "EUR", lang: str = "en") -> str:
    if minor is None:
        return "—"
    v = minor / 100
    body = num(v, lang, 0 if float(v).is_integer() else 2)
    sym = CUR.get(currency, currency)
    if lang == "en" and currency in ("EUR", "USD", "GBP"):
        return ("-" if v < 0 else "") + sym + body.lstrip("-")
    return f"{body} {sym}"


def date(iso: str, lang: str = "en") -> str:
    if not iso:
        return "—"
    d = dt.date.fromisoformat(iso[:10])
    if lang == "en":
        return f"{MONTHS['en'][d.month - 1]} {d.day}, {d.year}"
    return f"{d.day} {MONTHS[lang][d.month - 1]} {d.year}"


def month(ym: str, lang: str = "en") -> str:
    y, m = ym.split("-")
    return f"{MONTHS[lang][int(m) - 1]} {y}"


def t(key: str, lang: str = "en", params: dict | None = None, currency: str = "EUR") -> str:
    s = table(lang).get(key) or table("en").get(key) or key
    for k, v in (params or {}).items():
        s = s.replace("{" + k + "}", fmt_value(v, lang, currency))
    return s


def fmt_value(v, lang: str = "en", currency: str = "EUR") -> str:
    if isinstance(v, dict):
        if "$m" in v:
            return money(v["$m"], currency, lang)
        if "$n" in v:
            return num(v["$n"], lang, 0 if float(v["$n"]).is_integer() else 1)
        if "$p" in v:
            return "—" if v["$p"] is None else num(v["$p"], lang, 1) + " %" if lang != "en" else ("—" if v["$p"] is None else f"{v['$p']:.1f}%")
        if "$d" in v:
            return date(v["$d"], lang)
        if "$mo" in v:
            return month(v["$mo"], lang)
        if "$e" in v:
            return v.get(lang) or v.get("en") or v["$e"]
        if "raw" in v:
            return str(v["raw"])
        if "$k" in v:
            return t(v["$k"], lang, v.get("p"), currency)
    if isinstance(v, float):
        return num(v, lang, 1)
    if isinstance(v, int):
        return num(v, lang)
    return "" if v is None else str(v)
