"""Normalization for European small-business data (en, fr, ro, de).

Money is stored as integer minor units (cents/bani). Dates are ISO Gregorian.
Name keys are diacritic- and order-insensitive so 'Ioana Popescu', 'POPESCU Ioana'
and 'Doamna Ioana Popescu' resolve to the same customer.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import Iterable, Optional

# --- Text ------------------------------------------------------------------

_CEDILLA = str.maketrans({"ş": "ș", "Ş": "Ș", "ţ": "ț", "Ţ": "Ț", "’": "'", "‘": "'", "`": "'", " ": " ",
                          " ": " ", " ": " ", "–": "-", "—": "-"})
_FOLD_EXTRA = str.maketrans({"ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "đ": "d", "ł": "l"})
TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "mme", "mlle", "m", "monsieur", "madame", "mademoiselle",
          "doamna", "dna", "d-na", "domnul", "dl", "d-l", "doamnei", "domnului", "herr", "frau", "hr", "fr"}


def norm_text(value) -> str:
    """Unicode-normalized, trimmed, single-spaced text (keeps case and diacritics)."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFC", str(value)).translate(_CEDILLA)
    return re.sub(r"\s+", " ", s).strip()


def fold(value) -> str:
    """Lowercase, diacritics removed: for matching only."""
    s = norm_text(value).lower().translate(_FOLD_EXTRA)
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def header_key(value) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold(value)).strip()


def compact_key(value) -> str:
    return header_key(value).replace(" ", "")


def name_key(value) -> str:
    """Order-insensitive key for person names, titles removed."""
    tokens = [t for t in re.split(r"[^a-z0-9]+", fold(value).replace("-", " ")) if t and t not in TITLES]
    return " ".join(sorted(tokens))


def display_name(value) -> str:
    s = norm_text(value)
    if not s:
        return ""
    parts = [p for p in s.split(" ") if fold(p).strip(".") not in TITLES] or s.split(" ")
    caps = [p for p in parts if len(p) > 1 and p.isupper() and p.replace("-", "").isalpha()]
    if caps and len(caps) < len(parts):
        # 'DUBOIS Marie' / 'POPESCU Ioana': the capitalised word is the family name -> 'Marie Dubois'
        parts = [p for p in parts if p not in caps] + [c[:1] + c[1:].lower() for c in caps]
    s = " ".join(parts)
    if s.isupper() or s.islower():
        s = " ".join(w[:1].upper() + w[1:].lower() if w else w for w in s.split(" "))
        s = re.sub(r"-(\w)", lambda m: "-" + m.group(1).upper(), s)
    return s


# --- Phones ------------------------------------------------------------------

COUNTRY_CODES = {"RO": "40", "FR": "33", "DE": "49", "GB": "44", "UK": "44", "US": "1", "AT": "43", "CH": "41",
                 "BE": "32", "IT": "39", "ES": "34", "NL": "31", "MD": "373"}


def phone_key(value, country: str = "RO") -> Optional[str]:
    """E.164 (+40712345678) or None. National numbers use the business country."""
    if value is None:
        return None
    raw = str(value).strip()
    if isinstance(value, float):
        raw = str(int(value))
    digits = re.sub(r"\D", "", raw)
    if not digits or len(digits) < 6:
        return None
    if raw.startswith("+"):
        out = digits
    elif digits.startswith("00"):
        out = digits[2:]
    elif digits.startswith("0"):
        cc = COUNTRY_CODES.get((country or "RO").upper(), "40")
        out = cc + digits[1:]
    else:
        cc = COUNTRY_CODES.get((country or "RO").upper(), "40")
        # Already has a country code (e.g. 40712...), or a national number missing its leading zero (Excel).
        out = digits if any(digits.startswith(c) and len(digits) >= 10 for c in COUNTRY_CODES.values()) else cc + digits
    return "+" + out if 8 <= len(out) <= 15 else None


# --- Money -------------------------------------------------------------------

_CURRENCY = re.compile(r"(€|eur(?:o|os)?|ron|lei|leu|\$|usd|£|gbp|chf|fr\.?|chf\.?)", re.I)


def parse_money(value) -> Optional[int]:
    """Amount in minor units (x100). Understands 1.234,50 / 1,234.50 / 1 234,50 / 1'234.50 / 1.5k / '350 lei'."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(float(value) * 100))
    s = norm_text(value).lower()
    s = _CURRENCY.sub("", s).strip()
    neg = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    mult = 1
    m = re.fullmatch(r"([\d.,\s']+)\s*(k|mii|tsd\.?|tausend)", s)
    if m:
        s, mult = m.group(1), 1000
    s = re.sub(r"[\s'()\-+]", "", s)
    if not s or not re.fullmatch(r"[\d.,]+", s):
        return None
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            s = parts[0] + "." + parts[1]                      # decimal
        elif all(len(p) == 3 for p in parts[1:]):
            s = "".join(parts)                                 # thousands groups
        elif len(parts) == 2:
            s = parts[0] + "." + parts[1]
        else:
            return None
    try:
        amount = float(s) * mult
    except ValueError:
        return None
    return int(round(-amount * 100 if neg else amount * 100))


CURRENCY_SYMBOL = {"EUR": "€", "RON": "lei", "USD": "$", "GBP": "£", "CHF": "CHF"}


def fmt_money(minor: Optional[int], currency: str = "EUR") -> str:
    """Neutral English formatting for model-facing text: '€1,234.50', '1,234.50 lei'."""
    if minor is None:
        return "—"
    value = minor / 100
    body = f"{abs(value):,.2f}".replace(".00", "")
    sign = "-" if value < 0 else ""
    sym = CURRENCY_SYMBOL.get(currency, currency)
    return f"{sign}{body} {sym}" if currency in ("RON", "CHF") else f"{sign}{sym}{body}"


# --- Dates -------------------------------------------------------------------

MONTHS = {
    # en
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    # fr
    "janvier": 1, "janv": 1, "fevrier": 2, "fevr": 2, "fev": 2, "mars": 3, "avril": 4, "avr": 4, "mai": 5, "juin": 6,
    "juillet": 7, "juil": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    # ro
    "ianuarie": 1, "ian": 1, "februarie": 2, "martie": 3, "aprilie": 4, "iunie": 6, "iun": 6, "iulie": 7, "iul": 7,
    "septembrie": 9, "octombrie": 10, "noiembrie": 11, "noi": 11, "decembrie": 12,
    # de
    "januar": 1, "jaenner": 1, "februar": 2, "maerz": 3, "marz": 3, "mrz": 3, "juni": 6, "juli": 7,
    "oktober": 10, "okt": 10, "dezember": 12, "dez": 12,
}
_NUMERIC = re.compile(r"^(\d{1,4})[./\-](\d{1,2})[./\-](\d{1,4})(?:[ T].*)?$")
_NAMED = re.compile(r"^(?:[a-z]+,?\s+)?(\d{1,2})\.?\s+([a-z]+)\.?\s+(\d{2,4})$|^([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$")


def _year(y: int) -> int:
    return y + 2000 if y < 100 else y


def _named_date(s: str) -> Optional[dt.date]:
    m = _NAMED.match(fold(s))
    if not m:
        return None
    try:
        if m.group(1):
            mon = MONTHS.get(m.group(2))
            return dt.date(_year(int(m.group(3))), mon, int(m.group(1))) if mon else None
        mon = MONTHS.get(m.group(4))
        return dt.date(int(m.group(6)), mon, int(m.group(5))) if mon else None
    except ValueError:
        return None


def _parts(value) -> Optional[tuple[int, int, int]]:
    m = _NUMERIC.match(norm_text(value))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def infer_date_order(values: Iterable) -> Optional[str]:
    """Column-level: 'YMD', 'DMY' or 'MDY'. European default is DMY when ambiguous."""
    candidates = {"YMD", "DMY", "MDY"}
    seen = False
    for v in values:
        if isinstance(v, (dt.date, dt.datetime)) or v in (None, ""):
            continue
        p = _parts(v)
        if not p:
            continue
        seen = True
        a, b, c = p
        if a > 31 or len(str(a)) == 4:
            candidates &= {"YMD"}
            continue
        candidates.discard("YMD")
        if a > 12:
            candidates.discard("MDY")
        if b > 12:
            candidates.discard("DMY")
    if not seen:
        return None
    for order in ("YMD", "DMY", "MDY"):
        if order in candidates:
            return order
    return None


def parse_date(value, order: Optional[str] = None) -> Optional[dt.date]:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    named = _named_date(norm_text(value))
    if named:
        return named
    p = _parts(value)
    if not p:
        return None
    order = order or infer_date_order([value]) or "DMY"
    a, b, c = p
    try:
        if order == "YMD":
            return dt.date(_year(a), b, c)
        if order == "DMY":
            return dt.date(_year(c), b, a)
        return dt.date(_year(c), a, b)
    except ValueError:
        return None


def parse_time(value) -> Optional[str]:
    if isinstance(value, dt.datetime):
        return value.strftime("%H:%M")
    if isinstance(value, dt.time):
        return value.strftime("%H:%M")
    m = re.search(r"(\d{1,2})[:h.](\d{2})", norm_text(value))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def parse_month(value) -> Optional[str]:
    """'YYYY-MM' from '2026-10', '10/2026', 'Oct 2026', 'octombrie 2026', a date or a full date string."""
    if isinstance(value, (dt.date, dt.datetime)):
        return f"{value.year:04d}-{value.month:02d}"
    s = fold(value)
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})[/\-.](\d{1,2})", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{4})", s)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"([a-z]+)\.?\s+(\d{4})", s)
    if m and m.group(1) in MONTHS:
        return f"{int(m.group(2)):04d}-{MONTHS[m.group(1)]:02d}"
    d = parse_date(value)
    return f"{d.year:04d}-{d.month:02d}" if d else None


def detect_lang(text: str) -> Optional[str]:
    """Very small heuristic used only to pick a UI language when none is given."""
    t = f" {fold(text)} "
    scores = {
        "fr": sum(w in t for w in (" le ", " la ", " les ", " est ", " pour ", " avec ", " une ", " aujourd")),
        "ro": sum(w in t for w in (" si ", " este ", " pentru ", " azi ", " astazi ", " lei ", " cu ", " clienta ")),
        "de": sum(w in t for w in (" der ", " die ", " und ", " ist ", " heute ", " mit ", " fur ", " ein ")),
        "en": sum(w in t for w in (" the ", " and ", " is ", " today ", " with ", " for ", " came ")),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] >= 2 else None
