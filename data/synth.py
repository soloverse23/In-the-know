"""Three fictional, deliberately messy small businesses + ground truth computed from clean data.

    salon_aurora   Bucharest hair & beauty salon   ro / RON   (booking export + paper notebook)
    kine_saone     Lyon physiotherapy practice     fr / EUR   (split first/last names, no-shows)
    kaffeehaus     Berlin café                     de / EUR   (item-level POS export, overlapping files)

Run: uv run python data/synth.py   -> data/demo/<id>/... and data/demo/<id>.truth.json
The ground truth never touches the engine: it is computed from the generator's clean records.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import random
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from openpyxl import Workbook

OUT = Path(__file__).parent / "demo"
TODAY = dt.date(2026, 11, 1)
START = dt.date(2025, 11, 1)
END = dt.date(2026, 10, 31)


def days(a: dt.date, b: dt.date):
    cur = a
    while cur <= b:
        yield cur
        cur += dt.timedelta(days=1)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def write_csv(path: Path, header, rows, delimiter=","):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=delimiter)
        w.writerow(header)
        w.writerows(rows)


def write_xlsx(path: Path, header, rows):
    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)


def month_key(d: dt.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def common_truth(visits, costs, offerings, today=TODAY, month="2026-10", prev="2026-09"):
    """visits: dicts with date, person, offering, staff, amount (minor), counted, qty, time."""
    def rev(m):
        return sum(v["amount"] for v in visits if v["counted"] and month_key(v["date"]) == m)

    oct_v = [v for v in visits if v["counted"] and month_key(v["date"]) == month]
    by_off, by_staff = Counter(), Counter()
    for v in oct_v:
        by_off[v["offering"]] += v["amount"]
        if v["staff"]:
            by_staff[v["staff"]] += v["amount"]
    cogs = sum(offerings[v["offering"]]["cost"] * v.get("qty", 1) for v in oct_v)
    cost_total = sum(c["amount"] for c in costs if c["month"] == month)
    r, p = rev(month), rev(prev)
    margins = {n: (o["price"] - o["cost"]) / o["price"] for n, o in offerings.items()}
    return {
        "today": today.isoformat(), "month": month, "prev_month": prev,
        "revenue": r, "revenue_prev": p, "change_pct": round((r - p) / p * 100, 1),
        "top_offering": by_off.most_common(1)[0][0], "top_offering_revenue": by_off.most_common(1)[0][1],
        "top_staff": by_staff.most_common(1)[0][0] if by_staff else None,
        "top_staff_revenue": by_staff.most_common(1)[0][1] if by_staff else None,
        "cost_of_goods": round(cogs), "costs": cost_total, "net_profit": round(r - cogs - cost_total),
        "best_margin_offering": max(margins, key=margins.get),
        "transaction_count": len([v for v in visits if v["counted"]]),
    }


# ======================================================================================
# 1. Salon Aurora, Bucharest (ro, RON)
# ======================================================================================

def salon(rng: random.Random) -> dict:
    base = OUT / "salon_aurora"
    services = {  # canonical RO name: (EN export name, price lei, material lei, minutes, weight)
        "Tuns": ("Haircut", 120, 5, 45, 30), "Vopsit": ("Hair color", 350, 90, 120, 22),
        "Șuvițe": ("Highlights", 550, 140, 150, 8), "Keratină": ("Keratin", 800, 260, 180, 5),
        "Coafat": ("Blow dry", 80, 4, 30, 14), "Manichiură": ("Manicure", 90, 12, 45, 9),
        "Pedichiură": ("Pedicure", 120, 16, 60, 6), "Pensat": ("Eyebrows", 40, 1, 15, 4),
        "Machiaj": ("Makeup", 300, 45, 60, 2),
    }
    notebook_variant = {"Tuns": ["tuns", "Tuns", "tunsoare"], "Vopsit": ["vopsit", "Vopsit par", "vopsit"],
                        "Șuvițe": ["suvite", "Șuvițe"], "Keratină": ["keratina", "Keratină"], "Coafat": ["coafat", "Coafat"],
                        "Manichiură": ["manichiura"], "Pedichiură": ["pedichiura"], "Pensat": ["pensat"], "Machiaj": ["machiaj"]}
    offerings = {k: {"price": v[1] * 100, "cost": v[2] * 100} for k, v in services.items()}
    staff = ["Andreea", "Bianca", "Cristina", "Diana"]
    CRISTINA_LEFT = dt.date(2026, 9, 20)
    first = ["Ioana", "Maria", "Elena", "Andreea", "Alexandra", "Ana", "Cristina", "Mihaela", "Raluca", "Gabriela",
             "Simona", "Roxana", "Oana", "Diana", "Irina", "Larisa", "Ștefania", "Teodora", "Corina", "Bianca", "Alina", "Camelia"]
    last = ["Popescu", "Ionescu", "Popa", "Dumitru", "Stan", "Stoica", "Gheorghe", "Matei", "Constantin", "Marin",
            "Țurcanu", "Lungu", "Munteanu", "Moldovan", "Șerban", "Neagu", "Rusu", "Diaconu", "Nistor", "Vasile"]
    people, used = [], set()
    pid = 0

    def new_person(since, in_list=True, name=None):
        nonlocal pid
        while name is None or name in used:
            name = f"{rng.choice(first)} {rng.choice(last)}"
        used.add(name)
        pid += 1
        p = {"id": pid, "name": name, "phone": f"07{rng.randint(20, 99)}{rng.randint(100000, 999999)}",
             "since": since, "in_list": in_list, "interval": rng.choice([21, 28, 35, 42, 56, 75]),
             "pref": rng.choices(list(services), [s[4] for s in services.values()])[0],
             "stylist": rng.choice(staff), "source": rng.choice(["Instagram", "Recomandare", "Google", "Trecere"])}
        people.append(p)
        return p

    # Existing clientele (before data window), incl. a known duplicate person and a same-name pair.
    ioana = new_person(dt.date(2023, 5, 14), name="Ioana Popescu")
    ioana.update(interval=30, pref="Vopsit", stylist="Bianca")
    twin_a = new_person(dt.date(2022, 3, 2), name="Maria Ionescu")
    twin_b = new_person(dt.date(2024, 8, 9), name="Maria Ionescu ")  # same displayed name, different person/phone
    twin_b["name"] = "Maria Ionescu"
    for _ in range(115):
        new_person(dt.date(2021, 1, 1) + dt.timedelta(days=rng.randint(0, 1700)))
    # Make Cristina's regulars a meaningful group.
    for p in people[3:40]:
        p["stylist"] = "Cristina"

    visits = []
    CAMPAIGN = (dt.date(2026, 2, 20), dt.date(2026, 3, 20))
    campaign_new = []
    for day in days(START, END):
        if day.weekday() == 6:
            continue
        # organic new customers
        if rng.random() < 0.12:
            new_person(day, in_list=day < dt.date(2026, 5, 1))
        if CAMPAIGN[0] <= day <= CAMPAIGN[1] and rng.random() < 0.85:
            p = new_person(day, in_list=True)
            p.update(pref="Vopsit", interval=rng.choice([35, 42, 999]))
            campaign_new.append(p["id"])
    for p in people:
        t = max(START, p["since"]) + dt.timedelta(days=rng.randint(0, 20) if p["since"] < START else 0)
        once = p["interval"] == 999 or (p["id"] in campaign_new and rng.random() < 0.8)
        while t <= END:
            if t.weekday() == 6:
                t += dt.timedelta(days=1)
                continue
            stylist = p["stylist"]
            if stylist == "Cristina" and t > CRISTINA_LEFT:
                break                                        # her regulars stop coming
            svc = p["pref"] if rng.random() < 0.75 else rng.choice(list(services))
            price = services[svc][1]
            if svc == "Vopsit" and CAMPAIGN[0] <= t <= CAMPAIGN[1]:
                price = round(price * 0.8)
            if rng.random() < 0.06:
                price += rng.choice([20, 30, 50])           # extras / tips added
            visits.append({"date": t, "person": p["id"], "offering": svc, "staff": stylist, "amount": price * 100,
                           "counted": True, "qty": 1})
            if once:
                break
            t += dt.timedelta(days=max(10, int(rng.gauss(p["interval"], p["interval"] * 0.15))))
    # Ioana: guaranteed regular history incl. notebook variants later.
    visits.sort(key=lambda v: (v["date"], v["person"]))
    by_id = {p["id"]: p for p in people}
    # twin_b only visits in the export half (has phone there).
    visits = [v for v in visits if not (v["person"] in (twin_a["id"], twin_b["id"]) and v["date"] >= dt.date(2026, 5, 1))]

    # --- files -------------------------------------------------------------------
    write_csv(base / "lista_preturi.csv", ["Serviciu", "Preț (lei)", "Cost materiale"],
              [[k, rng.choice([f"{v[1]},00 lei", f"{v[1]} lei", str(v[1])]), f"{v[2]},00"] for k, v in services.items()])

    def phone_fmt(ph):
        return rng.choice([f"{ph[:4]} {ph[4:7]} {ph[7:]}", f"+40 {ph[1:4]} {ph[4:7]} {ph[7:]}", f"{ph[:4]}-{ph[4:7]}-{ph[7:]}", ph])

    listed = [p for p in people if p["in_list"]]
    rows = []
    for p in listed:
        rows.append([p["name"], phone_fmt(p["phone"]), p["since"].strftime("%d.%m.%Y"),
                     "vopsea 7.1, alergie amoniac" if p is ioana else rng.choice(["", "", "", "preferă dimineața", "plătește cash"]),
                     p["source"]])
    rows.append(["POPESCU IOANA", f"+40 {ioana['phone'][1:4]} {ioana['phone'][4:7]} {ioana['phone'][7:]}", "", "", "Instagram"])
    rng.shuffle(rows)
    write_xlsx(base / "clienti.xlsx", ["Nume și prenume", "Telefon", "Client din", "Observații", "Sursa"], rows)

    split = dt.date(2026, 5, 1)
    exp_rows, nb_rows = [], []
    for v in visits:
        p = by_id[v["person"]]
        if v["date"] < split:
            exp_rows.append([v["date"].isoformat(), p["name"], phone_fmt(p["phone"]), services[v["offering"]][0], v["staff"],
                             f"{v['amount'] / 100:.2f}"])
        else:
            nm = p["name"]
            style = rng.random()
            if p is ioana:
                nm = rng.choice(["Ioana Popescu", "Doamna Ioana Popescu", "Popescu Ioana", "ioana popescu"])
            elif style < 0.15:
                nm = strip_accents(nm)
            elif style < 0.25:
                nm = " ".join(reversed(nm.split()))
            elif style < 0.3:
                nm = "Dna " + nm
            amt = v["amount"] / 100
            money = rng.choice([f"{amt:.0f}", f"{amt:.0f} lei", f"{amt:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")])
            nb_rows.append([v["date"].strftime("%d/%m/%Y"), nm, rng.choice(notebook_variant[v["offering"]]), v["staff"], money])
    write_csv(base / "export_programari.csv", ["Date", "Client", "Phone", "Service", "Stylist", "Amount (RON)"], exp_rows)
    write_xlsx(base / "caiet_vizite.xlsx", ["Data", "Clienta", "Serviciu", "Stilist", "Suma"], nb_rows)

    ro_months = ["ianuarie", "februarie", "martie", "aprilie", "mai", "iunie", "iulie", "august", "septembrie", "octombrie",
                 "noiembrie", "decembrie"]
    costs = []
    m = START
    while m <= END:
        mk = month_key(m)
        for cat, amt in (("Chirie", 6500), ("Salarii", 14500 if m < CRISTINA_LEFT.replace(day=1) else 11000),
                         ("Utilități", rng.randint(900, 1400)), ("Produse consumabile", rng.randint(600, 900))):
            costs.append({"month": mk, "category": cat, "amount": amt * 100})
        m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    write_csv(base / "cheltuieli.csv", ["Luna", "Categorie", "Suma"],
              [[f"{ro_months[int(c['month'][5:]) - 1]} {c['month'][:4]}", c["category"],
                f"{c['amount'] / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")] for c in costs])
    (base / "note.md").write_text(
        "# Despre salon\nSalon Aurora, deschis în 2019 în Dorobanți. Lucrăm cu programare, mai ales pentru vopsit și keratină.\n\n"
        "# Obiective\n- Vreau să ajung la 40.000 lei pe lună până la Crăciun.\n- Clientele fidele sunt mai importante decât clientele noi.\n\n"
        "# Campanii\nÎntre 20 februarie și 20 martie 2026 am avut 20% reducere la vopsit pentru cliente noi (Instagram).\n\n"
        "# Echipa\nCristina a plecat la sfârșitul lui septembrie 2026; clientele ei nu au fost preluate de nimeni.\n",
        encoding="utf-8")

    # --- truth ---------------------------------------------------------------------
    truth = common_truth(visits, costs, offerings)
    dates = defaultdict(set)
    for v in visits:
        dates[v["person"]].add(v["date"])
    lapsed = [pid_ for pid_, ds in dates.items() if len(ds) >= 3 and (TODAY - max(ds)).days >= 60]
    spend = Counter()
    for v in visits:
        spend[v["person"]] += v["amount"]
    lapsed.sort(key=lambda i: -spend[i])
    first_visit = {i: min(ds) for i, ds in dates.items()}
    seen = {p["id"]: (min(p["since"], first_visit[p["id"]]) if p["in_list"] and p["id"] in first_visit else first_visit.get(p["id"]))
            for p in people}
    camp = [i for i, s in seen.items() if s and CAMPAIGN[0] <= s <= CAMPAIGN[1] and i in dates]
    truth.update({
        "vertical": "salon", "locale": "ro", "currency": "RON", "country": "RO", "name": "Salon Aurora",
        "customers": len(dates.keys() | {p["id"] for p in listed}),
        "lapsed_regulars": len(lapsed), "lapsed_top3": [by_id[i]["name"] for i in lapsed[:3]],
        "lapsed_last_staff_left": sum(1 for i in lapsed if by_id[i]["stylist"] == "Cristina"),
        "left_staff": "Cristina",
        "duplicate_person": {"name": "Ioana Popescu", "visits": len(dates[ioana["id"]]), "spend": spend[ioana["id"]],
                             "last_visit": max(dates[ioana["id"]]).isoformat()},
        "campaign": {"start": CAMPAIGN[0].isoformat(), "end": CAMPAIGN[1].isoformat(), "new": len(camp),
                     "returned": sum(1 for i in camp if len(dates[i]) > 1)},
        "new_field": "sursa",
    })
    return truth


# ======================================================================================
# 2. Cabinet Kiné Saône, Lyon (fr, EUR)
# ======================================================================================

def clinic(rng: random.Random) -> dict:
    base = OUT / "kine_saone"
    acts = {"Bilan kiné": (60.0, 2.0), "Séance de rééducation": (45.0, 1.5), "Massage thérapeutique": (55.0, 3.0),
            "Drainage lymphatique": (50.0, 2.0), "Séance à domicile": (65.0, 6.0)}
    offerings = {k: {"price": int(v[0] * 100), "cost": int(v[1] * 100)} for k, v in acts.items()}
    staff = ["Claire Martin", "Hugo Bernard"]
    JULIEN_START = dt.date(2026, 6, 1)
    SMS_STOP = dt.date(2026, 9, 1)
    first = ["Marie", "Camille", "Léa", "Chloé", "Manon", "Sophie", "Julie", "Thomas", "Lucas", "Hugo", "Nathalie",
             "Isabelle", "Pierre", "Jean", "Élodie", "François", "Anaïs", "Hélène", "Gérard", "Céline"]
    last = ["Dubois", "Moreau", "Laurent", "Simon", "Michel", "Lefèvre", "Leroy", "Roux", "David", "Bertrand", "Morel",
            "Fournier", "Girard", "Bonnet", "Dupont", "Lambert", "Fontaine", "Rousseau", "Vincent", "Muller"]
    patients, used = [], set()
    for i in range(140):
        while True:
            f, l_ = rng.choice(first), rng.choice(last)
            if (f, l_) not in used:
                used.add((f, l_))
                break
        patients.append({"id": i + 1, "first": f, "last": l_, "phone": f"06{rng.randint(10000000, 99999999)}",
                         "dob": dt.date(1940, 1, 1) + dt.timedelta(days=rng.randint(0, 25000)),
                         "mutuelle": rng.choice(["MGEN", "Harmonie", "AXA", "Malakoff", ""]),
                         "start": START + dt.timedelta(days=rng.randint(0, 330)), "course": rng.randint(4, 14),
                         "every": rng.choice([3, 4, 7, 7, 14]), "pract": rng.choice(staff)})
    appts = []
    for p in patients:
        t = p["start"]
        for k in range(p["course"]):
            if t > END:
                break
            while t.weekday() >= 5:
                t += dt.timedelta(days=1)
            act = "Bilan kiné" if k == 0 else rng.choices(list(acts), [0, 70, 12, 8, 10])[0]
            pract = p["pract"]
            if t >= JULIEN_START and rng.random() < 0.35:
                pract = "Julien Petit"
            ns_rate = 0.05 if t < SMS_STOP else 0.13
            r = rng.random()
            status = "no_show" if r < ns_rate else ("cancelled" if r < ns_rate + 0.04 else "done")
            hour = rng.choice([8, 9, 10, 11, 14, 15, 16, 17, 18])
            appts.append({"date": t, "time": f"{hour:02d}:{rng.choice(['00', '30'])}", "person": p["id"], "offering": act,
                          "staff": pract, "amount": offerings[act]["price"] if status == "done" else 0,
                          "counted": status == "done", "status": status, "qty": 1})
            t += dt.timedelta(days=p["every"])
    # A patient with no phone in the list but a returning name -> resolution by first+last
    by_id = {p["id"]: p for p in patients}

    write_csv(base / "tarifs.csv", ["Acte", "Tarif", "Coût consommables"],
              [[k, f"{v[0]:.2f}".replace(".", ",") + " €", f"{v[1]:.2f}".replace(".", ",")] for k, v in acts.items()], delimiter=";")

    def tel(ph):
        return rng.choice([f"{ph[:2]} {ph[2:4]} {ph[4:6]} {ph[6:8]} {ph[8:]}", f"+33 {ph[1]} {ph[2:4]} {ph[4:6]} {ph[6:8]} {ph[8:]}", ph])

    write_csv(base / "patients.csv", ["Nom", "Prénom", "Téléphone", "Date de naissance", "Remarques", "Mutuelle"],
              [[p["last"].upper() if rng.random() < 0.5 else p["last"], p["first"], tel(p["phone"]), p["dob"].strftime("%d/%m/%Y"),
                rng.choice(["", "", "lombalgie chronique", "post-opératoire genou", "sportif"]), p["mutuelle"]] for p in patients],
              delimiter=";")
    statut = {"done": ["honoré", "Honoré", "venu"], "no_show": ["absent", "Absent", "non venu"], "cancelled": ["annulé", "Annulé"]}
    rows = []
    for a in appts:
        p = by_id[a["person"]]
        name = rng.choice([f"{p['last'].upper()} {p['first']}", f"{p['first']} {p['last']}", f"Mme {p['first']} {p['last']}"
                           if p["first"] in ("Marie", "Camille", "Léa", "Chloé", "Manon", "Sophie", "Julie", "Nathalie", "Isabelle", "Élodie", "Anaïs", "Hélène", "Céline") else f"{p['first']} {p['last']}"])
        amount = f"{a['amount'] / 100:.2f}".replace(".", ",") + " €" if a["amount"] else "0"
        rows.append([a["date"].strftime("%d/%m/%Y"), a["time"].replace(":", "h"), name, a["staff"], a["offering"], amount,
                     rng.choice(statut[a["status"]])])
    write_xlsx(base / "rendez_vous.xlsx", ["Date", "Heure", "Patient", "Praticien", "Acte", "Montant", "Statut"], rows)
    costs = []
    m = START
    while m <= END:
        for cat, amt in (("Loyer cabinet", 1800), ("Logiciel & SMS", 90 if m < SMS_STOP else 45), ("Assurance RCP", 120),
                         ("Fournitures", rng.randint(80, 160)), ("Rétrocession Julien", 0 if m < JULIEN_START else 1500)):
            if amt:
                costs.append({"month": month_key(m), "category": cat, "amount": amt * 100})
        m = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    write_csv(base / "depenses.csv", ["Mois", "Libellé", "Montant"],
              [[f"{c['month'][5:]}/{c['month'][:4]}", c["category"], f"{c['amount'] / 100:.2f}".replace(".", ",")] for c in costs], delimiter=";")
    (base / "notes.md").write_text(
        "# Le cabinet\nCabinet de kinésithérapie à Lyon 2e, deux associés (Claire et Hugo).\n\n"
        "# Objectifs\n- Passer sous 6 % de rendez-vous non honorés.\n- Développer les séances à domicile.\n\n"
        "# Équipe\nJulien Petit a rejoint le cabinet en juin 2026 comme assistant (rétrocession 1 500 € / mois).\n\n"
        "# Rappels SMS\nOn a arrêté l'abonnement aux rappels SMS début septembre 2026 pour économiser.\n", encoding="utf-8")

    truth = common_truth(appts, costs, offerings)
    oct_status = [a for a in appts if month_key(a["date"]) == "2026-10"]
    sep_status = [a for a in appts if month_key(a["date"]) == "2026-08"]
    truth.update({
        "vertical": "clinic", "locale": "fr", "currency": "EUR", "country": "FR", "name": "Cabinet Kiné Saône",
        "customers": len({a["person"] for a in appts} | {p["id"] for p in patients}),
        "no_show_rate": round(sum(a["status"] == "no_show" for a in oct_status) / len(oct_status) * 100, 1),
        "no_show_rate_aug": round(sum(a["status"] == "no_show" for a in sep_status) / len(sep_status) * 100, 1),
        "no_shows_oct": sum(a["status"] == "no_show" for a in oct_status),
        "new_field": "mutuelle",
    })
    return truth


# ======================================================================================
# 3. Kaffeehaus Mitte, Berlin (de, EUR)
# ======================================================================================

def cafe(rng: random.Random) -> dict:
    base = OUT / "kaffeehaus"
    products = {"Espresso": ("Kaffee", 2.40, 0.35, 14), "Cappuccino": ("Kaffee", 3.60, 0.60, 22),
                "Latte Macchiato": ("Kaffee", 3.90, 0.70, 14), "Flat White": ("Kaffee", 3.90, 0.65, 10),
                "Chai Latte": ("Tee", 4.10, 0.80, 6), "Croissant": ("Backwaren", 2.80, 0.90, 14),
                "Käsekuchen": ("Kuchen", 4.20, 1.30, 7), "Bagel": ("Snacks", 5.50, 1.80, 6), "Wasser": ("Getränke", 2.50, 0.40, 7)}
    offerings = {k: {"price": round(v[1] * 100), "cost": round(v[2] * 100)} for k, v in products.items()}
    staff = ["Lena", "Jonas", "Mia"]
    BAGEL_START = dt.date(2026, 9, 1)
    regulars = [{"id": i + 1, "name": n, "phone": f"0151{rng.randint(1000000, 9999999)}", "card": f"KM-{1000 + i}"}
                for i, n in enumerate([f"{a} {b}" for a, b in zip(
                    ["Anna", "Paul", "Lea", "Felix", "Sophie", "Max", "Emma", "Leon", "Hannah", "Noah", "Lina", "Elias",
                     "Clara", "Ben", "Marie", "Finn", "Laura", "Jan", "Julia", "Tim"] * 2,
                    ["Schmidt", "Müller", "Weber", "Wagner", "Becker", "Hoffmann", "Schulz", "Koch", "Richter", "Klein",
                     "Wolf", "Neumann", "Schwarz", "Zimmermann", "Braun", "Krüger", "Hartmann", "Lange", "Werner", "Krause"] * 2)][:30])]
    sales = []
    receipt = 50000
    start = dt.date(2026, 8, 1)
    for day in days(start, END):
        weekend = day.weekday() >= 5
        base_n = 75 if not weekend else 95
        if dt.date(2026, 10, 6) <= day <= dt.date(2026, 10, 25):
            base_n = int(base_n * 0.72)                    # street construction in front of the café
        for _ in range(int(rng.gauss(base_n, 8))):
            receipt += 1
            hour = rng.choices(range(7, 19), [6, 14, 10, 7, 8, 9, 8, 7, 8, 6, 4, 2])[0]
            minute = rng.randint(0, 59)
            reg = rng.choice(regulars) if rng.random() < 0.18 else None
            barista = staff[(hour >= 13) + (weekend and hour >= 10)] if not weekend else rng.choice(staff)
            items = rng.choices(list(products), [p[3] if not (n == "Bagel" and day < BAGEL_START) else 0 for n, p in products.items()],
                                k=rng.choice([1, 1, 1, 2, 2, 3]))
            for prod, qty in Counter(items).items():
                sales.append({"date": day, "time": f"{hour:02d}:{minute:02d}", "receipt": str(receipt), "offering": prod,
                              "qty": qty, "amount": offerings[prod]["price"] * qty, "staff": barista,
                              "person": reg["id"] if reg else None, "counted": True})
    # Two overlapping exports (Aug 1 - Sep 30, Sep 24 - Oct 31) as the POS produces them.
    def money(x):
        return f"{x / 100:.2f}".replace(".", ",")
    card = {r["id"]: r["card"] for r in regulars}
    head = ["Datum", "Uhrzeit", "Bon-Nr", "Produkt", "Menge", "Betrag", "Mitarbeiter", "Kundenkarte"]

    def row(s):
        return [s["date"].strftime("%d.%m.%Y"), s["time"], s["receipt"], s["offering"], s["qty"], money(s["amount"]), s["staff"],
                card.get(s["person"], "")]
    write_csv(base / "kasse_export_aug_sep.csv", head, [row(s) for s in sales if s["date"] <= dt.date(2026, 9, 30)], delimiter=";")
    write_csv(base / "kasse_export_sep_okt.csv", head, [row(s) for s in sales if s["date"] >= dt.date(2026, 9, 24)], delimiter=";")
    write_csv(base / "produkte.csv", ["Produkt", "Kategorie", "Preis", "Wareneinsatz"],
              [[k, v[0], money(round(v[1] * 100)) + " €", money(round(v[2] * 100))] for k, v in products.items()], delimiter=";")
    write_csv(base / "stammkunden.csv", ["Name", "Handy", "Kartennr."],
              [[r["name"], r["phone"], r["card"]] for r in regulars], delimiter=";")
    de_months = ["Jan", "Feb", "Mär", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"]
    costs = []
    for mk in ("2026-08", "2026-09", "2026-10"):
        for cat, amt in (("Miete", 3200), ("Personal", 9800), ("Wareneinkauf Sonstiges", rng.randint(300, 500)), ("Strom & Wasser", 420)):
            costs.append({"month": mk, "category": cat, "amount": amt * 100})
    write_csv(base / "kosten.csv", ["Monat", "Posten", "Betrag"],
              [[f"{de_months[int(c['month'][5:]) - 1]} {c['month'][:4]}", c["category"], money(c["amount"])] for c in costs], delimiter=";")
    (base / "notizen.md").write_text(
        "# Über uns\nKaffeehaus Mitte, Specialty Coffee in der Torstraße seit 2021.\n\n"
        "# Ziele\n- Durchschnittsbon über 6 € bringen.\n- Mehr Stammgäste über die Kundenkarte.\n\n"
        "# Ereignisse\nVom 6. bis 25. Oktober 2026 Baustelle vor der Tür, Gehweg teilweise gesperrt.\n"
        "Seit September 2026 gibt es Bagels.\n", encoding="utf-8")

    # truth: revenue by item rows; receipts and avg ticket by receipt
    for s in sales:
        s["counted"] = True
    truth = common_truth(sales, costs, offerings)
    oct_s = [s for s in sales if month_key(s["date"]) == "2026-10"]
    receipts = len({(s["date"], s["receipt"]) for s in oct_s})
    hours = Counter()
    for s in oct_s:
        hours[int(s["time"][:2])] += s["amount"]
    truth.update({
        "vertical": "cafe", "locale": "de", "currency": "EUR", "country": "DE", "name": "Kaffeehaus Mitte",
        "receipts": receipts, "avg_ticket": round(truth["revenue"] / receipts),
        "busiest_hour": hours.most_common(1)[0][0], "items_sold": sum(s["qty"] for s in oct_s),
        "rows_total": len(sales), "regulars": len(regulars),
        "bagel_revenue_oct": sum(s["amount"] for s in oct_s if s["offering"] == "Bagel"),
    })
    return truth


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in (("salon_aurora", salon), ("kine_saone", clinic), ("kaffeehaus", cafe)):
        truth = fn(random.Random(42))
        (OUT / f"{name}.truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
        print(name, {k: truth[k] for k in ("revenue", "revenue_prev", "change_pct", "top_offering", "net_profit")})


if __name__ == "__main__":
    main()
