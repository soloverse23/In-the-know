"""Vertical packs: the starting schema, vocabulary, KPIs and layout for a trade.

A pack is only a starting point. The assistant can extend it at runtime (new fields,
new entities such as 'pet' or 'membership') through the schema engine.
Synonyms cover en / fr / ro / de headers as owners actually write them.
"""
from __future__ import annotations

from copy import deepcopy

L = lambda en, fr, ro, de: {"en": en, "fr": fr, "ro": ro, "de": de}  # noqa: E731

# --- Shared header synonyms ---------------------------------------------------------------
S_PERSON = ["name", "full name", "customer", "client", "customer name", "client name", "nume", "nume si prenume",
            "nume prenume", "clienta", "nume client", "nom", "nom complet", "nom et prenom", "nom du client", "cliente",
            "kunde", "kundin", "kundenname", "vor und nachname"]
S_FIRST = ["first name", "firstname", "given name", "prenume", "prenom", "vorname"]
S_LAST = ["last name", "lastname", "surname", "family name", "nume de familie", "nom de famille", "nachname", "familienname"]
S_PHONE = ["phone", "mobile", "tel", "telephone", "phone number", "mobile number", "cell", "telefon", "nr telefon",
           "numar telefon", "telefon mobil", "mobil", "portable", "numero", "numero de telephone", "tel portable",
           "handy", "telefonnummer", "mobilnummer", "handynummer", "rufnummer"]
S_EMAIL = ["email", "e mail", "mail", "adresa email", "courriel", "adresse mail", "e mail adresse"]
S_FIRST_SEEN = ["first visit", "client since", "customer since", "since", "member since", "data primei vizite",
                "prima vizita", "client din", "client de la", "premiere visite", "depuis", "client depuis", "date premiere visite",
                "erster besuch", "kunde seit", "seit", "mitglied seit"]
S_NOTES = ["notes", "note", "comments", "comment", "remarks", "observatii", "mentiuni", "notite", "comentarii",
           "remarques", "commentaire", "commentaires", "observations", "notizen", "bemerkung", "bemerkungen", "anmerkungen", "kommentar"]
S_STAFF = ["staff", "employee", "stylist", "hairdresser", "therapist", "practitioner", "barista", "served by", "by",
           "angajat", "stilist", "stilista", "coafeza", "frizer", "terapeut", "personal", "employe", "coiffeur", "coiffeuse",
           "praticien", "praticienne", "intervenant", "therapeute", "serveur", "mitarbeiter", "mitarbeiterin", "friseur",
           "friseurin", "therapeut", "behandler", "bedienung"]
S_DATE = ["date", "day", "visit date", "data", "ziua", "zi", "data vizitei", "data programarii", "jour", "date du rdv",
          "date rdv", "date de visite", "datum", "tag", "termin", "besuchsdatum"]
S_TIME = ["time", "hour", "ora", "heure", "horaire", "uhrzeit", "zeit"]
S_AMOUNT = ["amount", "total", "paid", "amount paid", "price paid", "revenue", "suma", "suma platita", "platit", "plata",
            "incasat", "valoare", "total plata", "montant", "paye", "montant paye", "regle", "prix paye", "betrag", "bezahlt",
            "summe", "umsatz", "gesamt", "preis"]
S_PRICE = ["price", "list price", "tariff", "rate", "pret", "pret lista", "tarif", "prix", "prix public", "preis",
           "listenpreis", "verkaufspreis"]
S_UNIT_COST = ["material cost", "materials", "cost of goods", "unit cost", "product cost", "cost materiale", "materiale",
               "cost produse", "cost unitar", "cout matiere", "cout produits", "cout unitaire", "materialkosten",
               "wareneinsatz", "einkaufspreis", "materialeinsatz", "consommables", "cout consommables", "cost consumabile",
               "consumabile", "consumables", "supplies cost", "verbrauchsmaterial"]
S_CATEGORY = ["category", "type", "group", "categorie", "tip", "grupa", "categorie", "famille", "rubrique", "kategorie",
              "gruppe", "warengruppe", "art"]
S_MONTH = ["month", "period", "luna", "perioada", "mois", "periode", "monat", "zeitraum"]
S_COST_CAT = ["category", "description", "item", "expense", "categorie", "descriere", "cheltuiala", "detalii", "categorie",
              "libelle", "poste", "nature", "depense", "kategorie", "beschreibung", "posten", "ausgabe", "kostenart"]
S_COST_AMT = ["amount", "cost", "total", "suma", "valoare", "cost", "montant", "cout", "total", "betrag", "kosten", "summe"]
S_QTY = ["quantity", "qty", "units", "cantitate", "buc", "bucati", "quantite", "qte", "menge", "anzahl", "stuck", "stk"]
S_STATUS = ["status", "state", "attended", "stare", "prezenta", "statut", "etat", "presence", "erschienen", "zustand"]

STATUS_VALUES = {
    "done": ["done", "attended", "completed", "yes", "ok", "prezent", "efectuat", "da", "venit", "honore", "venu",
             "present", "effectue", "oui", "erschienen", "erledigt", "ja", "wahrgenommen"],
    "no_show": ["no show", "noshow", "no_show", "absent", "missed", "nu a venit", "neprezentat", "absent", "non venu",
                "lapin", "pas venu", "nicht erschienen", "ausgefallen", "verpasst"],
    "cancelled": ["cancelled", "canceled", "anulat", "anulata", "annule", "annulee", "abgesagt", "storniert"],
}


def _party(name, label, extra_fields=()):
    return {"name": name, "role": "party", "label": label, "fields": [
        {"name": "name", "type": "text", "role_attr": "name", "required": True, "synonyms": S_PERSON,
         "parts": {"first": S_FIRST, "last": S_LAST}},
        {"name": "phone", "type": "phone", "role_attr": "phone", "synonyms": S_PHONE},
        {"name": "email", "type": "email", "role_attr": "email", "synonyms": S_EMAIL},
        {"name": "first_seen", "type": "date", "role_attr": "first_seen", "synonyms": S_FIRST_SEEN},
        {"name": "notes", "type": "text", "role_attr": "notes", "synonyms": S_NOTES},
        *extra_fields]}


def _staff(name, label, syn):
    return {"name": name, "role": "staff", "label": label, "fields": [
        {"name": "name", "type": "text", "role_attr": "name", "required": True, "synonyms": syn + S_STAFF}]}


def _offering(name, label, syn):
    return {"name": name, "role": "offering", "label": label, "fields": [
        {"name": "name", "type": "text", "role_attr": "name", "required": True, "synonyms": syn},
        {"name": "price", "type": "money", "role_attr": "price", "synonyms": S_PRICE},
        {"name": "unit_cost", "type": "money", "role_attr": "unit_cost", "synonyms": S_UNIT_COST},
        {"name": "category", "type": "text", "role_attr": "category", "synonyms": S_CATEGORY}]}


def _expense():
    return {"name": "expense", "role": "cost", "label": L(["Expense", "Expenses"], ["Dépense", "Dépenses"],
                                                       ["Cheltuială", "Cheltuieli"], ["Ausgabe", "Ausgaben"]),
            "fields": [
                {"name": "month", "type": "month", "role_attr": "month", "required": True, "synonyms": S_MONTH},
                {"name": "category", "type": "text", "role_attr": "category", "synonyms": S_COST_CAT},
                {"name": "amount", "type": "money", "role_attr": "amount", "required": True, "synonyms": S_COST_AMT}]}


def _ref(name, ref, attr, sub):
    return {"name": name, "type": "ref", "ref": ref, "role_attr": attr, "sub": sub}


S_SERVICE = ["service", "services", "treatment", "procedure", "serviciu", "servicii", "prestatie", "procedura",
             "tratament", "prestation", "soin", "service", "acte", "traitement", "dienstleistung", "leistung", "behandlung"]
S_PRODUCT = ["product", "item", "article", "produs", "articol", "produit", "article", "produkt", "artikel", "ware"]

SALON = {
    "id": "salon",
    "label": L("Hair & beauty salon", "Salon de coiffure & beauté", "Salon de înfrumusețare", "Friseur- & Kosmetiksalon"),
    "entities": [
        _party("customer", L(["Customer", "Customers"], ["Cliente", "Clientes"], ["Clientă", "Clienți"], ["Kundin", "Kunden"])),
        _staff("staff", L(["Stylist", "Team"], ["Coiffeur", "Équipe"], ["Stilist", "Echipă"], ["Friseur", "Team"]), []),
        _offering("service", L(["Service", "Services"], ["Prestation", "Prestations"], ["Serviciu", "Servicii"],
                               ["Leistung", "Leistungen"]), S_SERVICE),
        {"name": "visit", "role": "transaction",
         "label": L(["Visit", "Visits"], ["Visite", "Visites"], ["Vizită", "Vizite"], ["Besuch", "Besuche"]),
         "fields": [
             {"name": "date", "type": "date", "role_attr": "date", "required": True, "synonyms": S_DATE},
             {"name": "time", "type": "time", "role_attr": "time", "synonyms": S_TIME},
             _ref("customer", "customer", "party", {"name": S_PERSON, "phone": S_PHONE, "first": S_FIRST, "last": S_LAST}),
             _ref("service", "service", "offering", {"name": S_SERVICE}),
             _ref("staff", "staff", "staff", {"name": S_STAFF}),
             {"name": "amount", "type": "money", "role_attr": "amount", "required": True, "synonyms": S_AMOUNT}]},
        _expense(),
    ],
    "offering_groups": [
        ["haircut", "cut", "tuns", "tunsoare", "coupe", "haarschnitt", "schneiden"],
        ["color", "colour", "hair color", "coloring", "vopsit", "vopsit par", "colorare", "coloration", "couleur", "farbe", "farben", "colorieren"],
        ["highlights", "balayage", "suvite", "meches", "strahnchen", "highlight"],
        ["keratin", "keratina", "tratament keratina", "keratine", "keratinbehandlung"],
        ["blow dry", "blowdry", "brushing", "coafat", "coafura", "brushing", "fohnen", "foehnen", "styling"],
        ["manicure", "manichiura", "manucure", "manikure"],
        ["pedicure", "pedichiura", "pedicure", "pedikure"],
        ["eyebrows", "brows", "eyebrow", "pensat", "sprancene", "sourcils", "augenbrauen"],
        ["makeup", "make up", "machiaj", "maquillage", "make-up"],
    ],
    "kpis": ["revenue", "transactions", "unique_customers", "net_profit"],
    "segments": ["due_soon", "lapsed_regulars", "at_risk", "top_spenders", "active", "one_time"],
    "brief_layout": ["kpis", "attention", "memory", "trend", "staff", "offerings", "notes", "quality"],
    "theme": {"primary": "#B4447A", "accent": "#F2A65A"},
    "tone": {"en": "warm and personal", "fr": "chaleureux et personnel", "ro": "cald și personal", "de": "herzlich und persönlich"},
}

CLINIC = {
    "id": "clinic",
    "label": L("Clinic / practice", "Cabinet / clinique", "Clinică / cabinet", "Praxis / Klinik"),
    "entities": [
        _party("patient", L(["Patient", "Patients"], ["Patient", "Patients"], ["Pacient", "Pacienți"], ["Patient", "Patienten"]),
               extra_fields=[{"name": "birth_date", "type": "date", "synonyms": ["birth date", "date of birth", "dob", "data nasterii",
                                                                                 "date de naissance", "geburtsdatum"]}]),
        _staff("practitioner", L(["Practitioner", "Practitioners"], ["Praticien", "Praticiens"], ["Medic", "Medici"],
                                 ["Behandler", "Behandler"]), ["doctor", "medic", "docteur", "kine", "kinesitherapeute", "arzt", "arztin"]),
        _offering("treatment", L(["Treatment", "Treatments"], ["Acte", "Actes"], ["Tratament", "Tratamente"],
                                 ["Behandlung", "Behandlungen"]), S_SERVICE + ["consultation", "consultatie", "beratung", "sitzung", "seance", "sedinta"]),
        {"name": "appointment", "role": "transaction",
         "label": L(["Appointment", "Appointments"], ["Rendez-vous", "Rendez-vous"], ["Programare", "Programări"], ["Termin", "Termine"]),
         "fields": [
             {"name": "date", "type": "date", "role_attr": "date", "required": True, "synonyms": S_DATE + ["rdv", "rendez vous"]},
             {"name": "time", "type": "time", "role_attr": "time", "synonyms": S_TIME},
             _ref("patient", "patient", "party", {"name": S_PERSON + ["patient", "pacient", "patientin"], "phone": S_PHONE,
                                                   "first": S_FIRST, "last": S_LAST}),
             _ref("treatment", "treatment", "offering", {"name": S_SERVICE + ["consultation", "consultatie", "seance", "sedinta", "sitzung"]}),
             _ref("practitioner", "practitioner", "staff", {"name": S_STAFF + ["doctor", "medic", "docteur", "arzt", "arztin"]}),
             {"name": "amount", "type": "money", "role_attr": "amount", "required": True, "synonyms": S_AMOUNT + ["honoraires", "onorariu", "honorar"]},
             {"name": "status", "type": "enum", "role_attr": "status", "values": list(STATUS_VALUES), "synonyms": S_STATUS,
              "value_synonyms": STATUS_VALUES}]},
        _expense(),
    ],
    "offering_groups": [
        ["consultation", "consult", "consultatie", "beratung", "erstberatung", "bilan", "first visit"],
        ["follow up", "follow-up", "control", "suivi", "seance de suivi", "kontrolle", "folgetermin"],
    ],
    "kpis": ["revenue", "transactions", "no_show_rate", "net_profit"],
    "segments": ["due_soon", "at_risk", "top_spenders", "active", "one_time"],   # finished treatment courses are not 'lost'

    "brief_layout": ["kpis", "attention", "memory", "trend", "staff", "offerings", "notes", "quality"],
    "theme": {"primary": "#2F6FDE", "accent": "#3DBEA0"},
    "tone": {"en": "calm and reassuring", "fr": "calme et rassurant", "ro": "calm și încurajator", "de": "ruhig und vertrauensvoll"},
}

CAFE = {
    "id": "cafe",
    "label": L("Café / restaurant", "Café / restaurant", "Cafenea / restaurant", "Café / Restaurant"),
    "entities": [
        _party("customer", L(["Regular", "Regulars"], ["Habitué", "Habitués"], ["Client fidel", "Clienți fideli"],
                              ["Stammgast", "Stammgäste"]),
               extra_fields=[{"name": "card", "type": "text", "synonyms": ["card", "loyalty card", "card id", "card nr", "card no",
                                                                           "carte", "carte fidelite", "kundenkarte", "karte", "karten nr", "kartennr", "kartennummer", "card fidelitate",
                                                                           "nr card", "numero carte", "member id", "member no"]}]),
        _staff("staff", L(["Staff", "Team"], ["Équipe", "Équipe"], ["Personal", "Echipă"], ["Mitarbeiter", "Team"]), []),
        _offering("product", L(["Product", "Menu"], ["Produit", "Carte"], ["Produs", "Meniu"], ["Produkt", "Karte"]), S_PRODUCT),
        {"name": "sale", "role": "transaction",
         "label": L(["Sale", "Sales"], ["Vente", "Ventes"], ["Vânzare", "Vânzări"], ["Verkauf", "Verkäufe"]),
         "fields": [
             {"name": "date", "type": "date", "role_attr": "date", "required": True, "synonyms": S_DATE + ["verkaufsdatum"]},
             {"name": "time", "type": "time", "role_attr": "time", "synonyms": S_TIME},
             _ref("product", "product", "offering", {"name": S_PRODUCT}),
             {"name": "quantity", "type": "number", "role_attr": "quantity", "synonyms": S_QTY},
             {"name": "amount", "type": "money", "role_attr": "amount", "required": True, "synonyms": S_AMOUNT},
             _ref("staff", "staff", "staff", {"name": S_STAFF}),
             _ref("customer", "customer", "party", {"name": ["customer", "client", "regular", "stammgast", "kunde"],
                                                     "phone": S_PHONE,
                                                     "card": ["card", "loyalty card", "card id", "carte", "carte fidelite",
                                                              "kundenkarte", "karte", "kartennr", "card fidelitate"]}),
             {"name": "receipt", "type": "text", "synonyms": ["receipt", "receipt no", "bon", "bon nr", "nr bon", "ticket",
                                                             "no ticket", "bon nr", "beleg", "beleg nr", "bonnummer"]},
             {"name": "channel", "type": "text", "synonyms": ["channel", "canal", "canal de vente", "kanal", "vertriebsweg",
                                                             "type commande", "service"]}]},
        _expense(),
    ],
    "offering_groups": [
        ["espresso", "expresso", "espresso simplu"], ["cappuccino", "capuccino", "cappucino"],
        ["latte", "cafe latte", "latte macchiato", "milchkaffee", "caffe latte"],
        ["croissant", "corn", "hornchen"], ["water", "apa", "eau", "wasser"],
    ],
    "kpis": ["revenue", "receipts", "avg_ticket", "net_profit"],
    "segments": ["top_spenders", "active", "lapsed_regulars", "one_time"],
    "brief_layout": ["kpis", "attention", "memory", "hours", "offerings", "staff", "notes", "quality"],
    "theme": {"primary": "#8A5A3B", "accent": "#E0B15C"},
    "tone": {"en": "short and friendly", "fr": "court et sympathique", "ro": "scurt și prietenos", "de": "kurz und freundlich"},
}

GENERIC = {
    "id": "generic",
    "label": L("Other small business", "Autre commerce", "Altă afacere", "Anderes Geschäft"),
    "entities": [
        _party("customer", L(["Customer", "Customers"], ["Client", "Clients"], ["Client", "Clienți"], ["Kunde", "Kunden"])),
        _staff("staff", L(["Staff", "Team"], ["Équipe", "Équipe"], ["Personal", "Echipă"], ["Mitarbeiter", "Team"]), []),
        _offering("offering", L(["Offering", "Offerings"], ["Offre", "Offres"], ["Ofertă", "Oferte"], ["Angebot", "Angebote"]),
                  S_SERVICE + S_PRODUCT),
        {"name": "transaction", "role": "transaction",
         "label": L(["Sale", "Sales"], ["Vente", "Ventes"], ["Vânzare", "Vânzări"], ["Verkauf", "Verkäufe"]),
         "fields": [
             {"name": "date", "type": "date", "role_attr": "date", "required": True, "synonyms": S_DATE},
             {"name": "time", "type": "time", "role_attr": "time", "synonyms": S_TIME},
             _ref("customer", "customer", "party", {"name": S_PERSON, "phone": S_PHONE, "first": S_FIRST, "last": S_LAST}),
             _ref("offering", "offering", "offering", {"name": S_SERVICE + S_PRODUCT}),
             _ref("staff", "staff", "staff", {"name": S_STAFF}),
             {"name": "quantity", "type": "number", "role_attr": "quantity", "synonyms": S_QTY},
             {"name": "amount", "type": "money", "role_attr": "amount", "required": True, "synonyms": S_AMOUNT}]},
        _expense(),
    ],
    "offering_groups": [],
    "kpis": ["revenue", "transactions", "unique_customers", "net_profit"],
    "segments": ["due_soon", "lapsed_regulars", "at_risk", "top_spenders", "active", "one_time"],
    "brief_layout": ["kpis", "attention", "memory", "trend", "staff", "offerings", "notes", "quality"],
    "theme": {"primary": "#4B5BDB", "accent": "#18B39B"},
    "tone": {"en": "friendly and clear", "fr": "aimable et clair", "ro": "prietenos și clar", "de": "freundlich und klar"},
}

PACKS = {p["id"]: p for p in (SALON, CLINIC, CAFE, GENERIC)}


def pack(vertical: str) -> dict:
    return deepcopy(PACKS.get(vertical, GENERIC))


def offering_groups(vertical: str) -> list[list[str]]:
    return PACKS.get(vertical, GENERIC)["offering_groups"]
