import datetime as dt

import pytest

from intheknow.normalize import display_name, infer_date_order, name_key, parse_date, parse_money, parse_month, phone_key


@pytest.mark.parametrize("raw,minor", [
    ("1.234,50 €", 123450), ("1,234.50", 123450), ("1 234,50", 123450), ("350 lei", 35000), ("1.250,00 lei", 125000),
    ("1'234.50 CHF", 123450), ("1.5k", 150000), ("45,00 €", 4500), ("3,20", 320), (45, 4500), (3.2, 320), ("-12,00", -1200), ("abc", None),
])
def test_money(raw, minor):
    assert parse_money(raw) == minor


def test_names_are_order_title_and_diacritic_insensitive():
    k = name_key("Ioana Popescu")
    assert name_key("POPESCU IOANA") == k == name_key("Doamna Ioana Popescu") == name_key("Dna. ioana popescu")
    assert name_key("Ştefania Ţiriac") == name_key("Ștefania Țiriac") == name_key("Stefania Tiriac")
    assert name_key("Mme Marie Dubois") == name_key("DUBOIS Marie")
    assert name_key("Jürgen Groß") == name_key("Jurgen Gross")
    assert display_name("DUBOIS Marie") == "Marie Dubois"


@pytest.mark.parametrize("raw,country,e164", [
    ("0722 123 456", "RO", "+40722123456"), ("+40 722 123 456", "FR", "+40722123456"), ("0040722123456", "DE", "+40722123456"),
    ("06 12 34 56 78", "FR", "+33612345678"), ("0151 2345678", "DE", "+491512345678"), ("12", "RO", None),
])
def test_phones(raw, country, e164):
    assert phone_key(raw, country) == e164


def test_dates_four_languages():
    assert infer_date_order(["31.10.2026", "01.11.2026"]) == "DMY"
    assert infer_date_order(["2026-10-31"]) == "YMD"
    assert parse_date("31.10.2026") == dt.date(2026, 10, 31)
    assert parse_date("12 martie 2026") == parse_date("12 mars 2026") == parse_date("12. März 2026") == parse_date("March 12, 2026") == dt.date(2026, 3, 12)
    assert parse_month("octombrie 2026") == parse_month("Okt 2026") == parse_month("10/2026") == parse_month("oct. 2026") == "2026-10"
