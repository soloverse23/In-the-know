"""Golden questions per demo business, in the owner's language, graded against the generator's ground truth."""
from __future__ import annotations

import json
from pathlib import Path

DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"


def money(minor: int, cur: str) -> str:
    v = minor / 100
    return f"{v:,.2f} {cur}"


def truth(name: str) -> dict:
    return json.loads((DEMO / f"{name}.truth.json").read_text(encoding="utf-8"))


def salon() -> list[dict]:
    g = truth("salon_aurora")
    c = "RON"
    return [
        {"id": "ro_revenue", "kind": "fact", "text": "Cât am încasat în octombrie 2026 și cum se compară cu septembrie?",
         "criteria": [f"States October revenue ≈ {money(g['revenue'], c)} (±1%)", f"States September ≈ {money(g['revenue_prev'], c)} (±1%)",
                      f"States a drop of about {abs(g['change_pct'])}% (±1 pt)"]},
        {"id": "ro_profit", "kind": "fact", "text": "Am avut profit în octombrie?",
         "criteria": [f"Says October was a loss of ≈ {money(-g['net_profit'], c)} (±2%)", "Explains it with materials and fixed costs (rent, salaries)"]},
        {"id": "ro_why_drop", "kind": "advice", "text": "De ce au scăzut încasările și ce ar trebui să fac săptămâna asta?",
         "criteria": [f"Identifies that {g['left_staff']} left and her clients stopped coming", f"Mentions about {g['lapsed_regulars']} lapsed regulars (±10%)",
                      "Names specific customers to contact", "Gives a concrete action plan"]},
        {"id": "ro_customer", "kind": "fact", "text": "Spune-mi tot despre Ioana Popescu.",
         "criteria": [f"{g['duplicate_person']['visits']} visits (±1)", f"Total spent ≈ {money(g['duplicate_person']['spend'], c)} (±2%)",
                      f"Last visit {g['duplicate_person']['last_visit']}", "Mentions the ammonia allergy / colour 7.1 note"]},
        {"id": "ro_campaign", "kind": "advice", "text": "Reducerea de 20% la vopsit din februarie-martie a meritat? S-o repet?",
         "criteria": [f"About {g['campaign']['new']} new customers in the campaign window (±20%)", f"Only about {g['campaign']['returned']} came back (±2)",
                      "Concludes the discount attracted one-time customers", "Recommends a different approach (e.g. retention of regulars)"]},
    ]


def clinic() -> list[dict]:
    g = truth("kine_saone")
    c = "EUR"
    return [
        {"id": "fr_revenue", "kind": "fact", "text": "Quel a été mon chiffre d'affaires en octobre 2026 par rapport à septembre ?",
         "criteria": [f"October ≈ {money(g['revenue'], c)} (±1%)", f"September ≈ {money(g['revenue_prev'], c)} (±1%)", f"Drop of about {abs(g['change_pct'])}% (±1 pt)"]},
        {"id": "fr_noshow", "kind": "advice", "text": "Les rendez-vous non honorés augmentent ? Pourquoi, et que faire ?",
         "criteria": [f"October no-show rate ≈ {g['no_show_rate']}% (±0.5 pt)", f"Notes it was about {g['no_show_rate_aug']}% in August or lower before",
                      "Links it to stopping the SMS reminders in September", "Recommends bringing reminders back with the cost/benefit"]},
        {"id": "fr_best", "kind": "fact", "text": "Quel praticien a fait le plus de chiffre en octobre ?",
         "criteria": [f"Names {g['top_staff']}", f"≈ {money(g['top_staff_revenue'], c)} (±2%)"]},
    ]


def cafe() -> list[dict]:
    g = truth("kaffeehaus")
    c = "EUR"
    return [
        {"id": "de_revenue", "kind": "fact", "text": "Wie war der Umsatz im Oktober 2026 im Vergleich zum September?",
         "criteria": [f"October ≈ {money(g['revenue'], c)} (±1%)", f"September ≈ {money(g['revenue_prev'], c)} (±1%)", f"Drop of about {abs(g['change_pct'])}% (±1 pt)"]},
        {"id": "de_ticket", "kind": "fact", "text": "Wie viele Bons hatten wir im Oktober und wie hoch war der Durchschnittsbon?",
         "criteria": [f"{g['receipts']} receipts (±1%)", f"Average ticket ≈ {money(g['avg_ticket'], c)} (±2%)"]},
        {"id": "de_why", "kind": "advice", "text": "Warum lief der Oktober schlechter und wann ist bei uns am meisten los?",
         "criteria": ["Mentions the street construction 6–25 October", f"Busiest hour is {g['busiest_hour']}:00", "Gives one concrete idea for the busy hour or the goal of a 6 € ticket"]},
    ]


BUSINESSES = {
    "salon_aurora": {"setup": dict(name="Salon Aurora", vertical="salon", locale="ro", currency="RON", country="RO"), "questions": salon},
    "kine_saone": {"setup": dict(name="Cabinet Kiné Saône", vertical="clinic", locale="fr", currency="EUR", country="FR"), "questions": clinic},
    "kaffeehaus": {"setup": dict(name="Kaffeehaus Mitte", vertical="cafe", locale="de", currency="EUR", country="DE"), "questions": cafe},
}
