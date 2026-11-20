"""First-run experience: connect -> profile -> project instructions -> import -> review -> try it.

The state is derived from what is actually in the database (not from flags the model sets),
so it is always truthful. Also imports old chats (Claude / ChatGPT exports) in batches the
assistant reads and turns into captures.
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Optional

from . import schema
from .db import get_setting, set_setting
from .normalize import fold
from .views import N, T, block, view

STEPS = ["connect", "profile", "instructions", "import", "review", "try_capture", "try_question", "try_design"]

PROJECT_INSTRUCTIONS = {
    "en": """You are the assistant of {name}. The In The Know connector is the business's memory.
- Start every conversation by quietly calling get_context. Show cards only when I ask to see something.
- Whenever I mention something about the business (a visit or sale, a customer detail, a price, an expense, a goal, a rule, a decision, a team change, a promise or appointment, a recurring cost like rent), save it immediately with the capture tool. Never say it is saved without the tool's result.
- What I tell you about a person (what they like, dislike, must have or refuse, their goals, health, family, who they are linked to) is saved as a fact or relation for that person. Before I meet or write to someone, check get_person_brief.
- Import files I share with add_data; read photos of notebooks or receipts and save the rows with import_records.
- Business numbers come only from In The Know tools, never from your own calculations.
- Answer in English, short and practical.""",
    "fr": """Tu es l'assistant de {name}. Le connecteur In The Know est la mémoire de l'entreprise.
- Commence chaque conversation en appelant discrètement get_context. N'affiche des cartes que si je demande à voir quelque chose.
- Dès que je mentionne quelque chose sur l'activité (une visite ou une vente, un détail client, un prix, une dépense, un objectif, une règle, une décision, un changement d'équipe, une promesse ou un rendez-vous, une charge récurrente comme le loyer), enregistre-le tout de suite avec l'outil capture. Ne dis jamais que c'est enregistré sans le résultat de l'outil.
- Ce que je te dis d'une personne (ce qu'elle aime, n'aime pas, exige ou refuse, ses objectifs, sa santé, sa famille, ses liens) s'enregistre comme fait ou relation de cette personne. Avant que je rencontre ou écrive à quelqu'un, consulte get_person_brief.
- Importe les fichiers que je partage avec add_data ; lis les photos de cahiers ou de tickets et enregistre les lignes avec import_records.
- Les chiffres de l'entreprise viennent uniquement des outils In The Know, jamais de tes propres calculs.
- Réponds en français, de façon courte et concrète.""",
    "ro": """Ești asistentul afacerii {name}. Conectorul In The Know este memoria afacerii.
- Începe fiecare conversație apelând discret get_context. Arată carduri doar când cer să văd ceva.
- De fiecare dată când menționez ceva despre afacere (o vizită sau o vânzare, un detaliu despre o clientă, un preț, o cheltuială, un obiectiv, o regulă, o decizie, o schimbare în echipă, o promisiune sau o întâlnire, o cheltuială lunară ca chiria), salvează imediat cu instrumentul capture. Nu spune niciodată că e salvat fără rezultatul instrumentului.
- Ce îți spun despre o persoană (ce îi place, ce nu îi place, ce cere sau refuză, obiectivele, sănătatea, familia, cu cine e legată) se salvează ca fapt sau relație a acelei persoane. Înainte să mă întâlnesc sau să scriu cuiva, verifică get_person_brief.
- Importă fișierele pe care le trimit cu add_data; citește pozele cu caiete sau bonuri și salvează rândurile cu import_records.
- Cifrele afacerii vin doar din instrumentele In The Know, niciodată din calculele tale.
- Răspunde în română, scurt și practic.""",
    "de": """Du bist der Assistent von {name}. Der Connector In The Know ist das Gedächtnis des Geschäfts.
- Rufe zu Beginn jedes Gesprächs unauffällig get_context auf. Zeige Karten nur, wenn ich etwas sehen möchte.
- Sobald ich etwas über das Geschäft erwähne (einen Besuch oder Verkauf, ein Kundendetail, einen Preis, eine Ausgabe, ein Ziel, eine Regel, eine Entscheidung, eine Änderung im Team, eine Zusage oder einen Termin, laufende Kosten wie die Miete), speichere es sofort mit dem Tool capture. Sag nie, dass etwas gespeichert ist, ohne das Ergebnis des Tools.
- Was ich dir über eine Person erzähle (was sie mag, nicht mag, unbedingt will oder ablehnt, ihre Ziele, Gesundheit, Familie, mit wem sie verbunden ist), wird als Fakt oder Beziehung dieser Person gespeichert. Bevor ich jemanden treffe oder anschreibe, prüfe get_person_brief.
- Importiere Dateien, die ich teile, mit add_data; lies Fotos von Heften oder Belegen und speichere die Zeilen mit import_records.
- Geschäftszahlen kommen nur aus den In-The-Know-Tools, nie aus deinen eigenen Berechnungen.
- Antworte auf Deutsch, kurz und praktisch.""",
}

BUSINESS_WORDS = ["client", "customer", "clienta", "clienti", "patient", "pacient", "kunde", "kundin", "rdv", "rendez", "programare",
                  "vizit", "visit", "venit", "came", "sale", "vente", "vanzare", "verkauf", "price", "pret", "prix", "preis", "cost",
                  "cheltuial", "depense", "kosten", "rent", "chirie", "loyer", "miete", "salary", "salariu", "salaire", "gehalt",
                  "staff", "angajat", "employe", "mitarbeiter", "lei", "eur", "€", "revenue", "chiffre", "incasari", "umsatz",
                  "discount", "reducere", "remise", "rabatt", "campaign", "campanie", "campagne", "kampagne", "goal", "obiectiv",
                  "objectif", "ziel", "no show", "absent", "annul", "anulat", "abgesagt"]


class OnboardingOps:
    """Mixin for ContextEngine."""

    def _state(self) -> dict[str, bool]:
        c = self.conn
        used = {r["tool"] for r in c.execute("SELECT DISTINCT tool FROM usage_log")}
        has_files = c.execute("SELECT 1 FROM raw_files LIMIT 1").fetchone() is not None
        open_q = c.execute("SELECT COUNT(*) FROM questions WHERE status='open'").fetchone()[0]
        return {
            "connect": True,
            "profile": self.is_setup,
            "instructions": get_setting(c, "onb_instructions") == "done",
            "import": has_files or self.has_data(),
            "review": (has_files or self.has_data()) and (open_q == 0 or get_setting(c, "onb_review") == "done"),
            "try_capture": "used:capture" in used,
            "try_question": bool({"used:review", "query_data", "find_customers", "prepare_decision_context"} & used),
            "try_design": "used:design" in used,
        }

    def get_started(self, step_done: Optional[str] = None) -> dict:
        if step_done in ("instructions", "review"):
            set_setting(self.conn, f"onb_{step_done}", "done")
            self.conn.commit()
        state = self._state()
        current = next((s for s in STEPS if not state[s]), None)
        items = [{"key": s, "t": f"onb.{s}", "state": "done" if state[s] else ("current" if s == current else "todo")} for s in STEPS]
        name = get_setting(self.conn, "name") or ""
        blocks = [block("hero", **T("onb.hero_named" if name else "onb.hero", name=name)),
                  block("checklist", items=items, progress=round(sum(state.values()) / len(STEPS), 2))]
        hints: list[str] = []
        if current == "profile":
            blocks.append(block("text", **T("onb.ask_profile")))
            hints.append("Ask the owner in their language: business name, type (salon, clinic, cafe, or describe it), city/country, "
                         "language (en/fr/ro/de) and currency. Then call setup_business. For a trade that is not salon/clinic/cafe "
                         "use vertical='generic' and then extend it with change_schema.")
        elif current == "instructions":
            lang = self.lang
            blocks.append(block("copy", title=T("onb.instructions_title"), text=PROJECT_INSTRUCTIONS[lang].format(name=name),
                                steps=[f"onb.instr_step{i}" for i in range(1, 5)]))
            hints.append("Explain why: without these Project instructions Claude may not save facts automatically. When the owner "
                         "has created the Project (or declines), call get_started(step_done='instructions').")
        elif current == "import":
            blocks.append(block("tips", items=[T("onb.tip_files"), T("onb.tip_photos"), T("onb.tip_history"), T("onb.tip_none")]))
            hints.append("Ask for a folder path or attachments (add_data). For photos, read them and call import_records. "
                         "For old chats: Settings > Privacy > Export data in Claude (or ChatGPT export), then import_chat_history(path).")
        elif current == "review":
            qs = [{"id": r["id"], "kind": r["kind"], "text": r["text"], "payload": json.loads(r["payload"])}
                  for r in self.conn.execute("SELECT * FROM questions WHERE status='open' ORDER BY id LIMIT 10")]
            blocks.append(block("questions", title=T("onb.review_title"), items=qs))
            hints.append("Go through the important questions with the owner (resolve_question / merge_customers / capture). "
                         "When done or the rest can wait, call get_started(step_done='review').")
        elif current and current.startswith("try_"):
            blocks.append(block("tips", title=T("onb.try_title"), items=[T("onb.try_capture_ex"), T("onb.try_question_ex"), T("onb.try_design_ex")]))
            hints.append("Invite the owner to try the next exercise in their own words; the checklist ticks itself.")
        else:
            blocks.append(block("text", **T("onb.done")))
            hints.append("Onboarding complete. Offer get_business_brief, get_action_list or a monthly report Artifact (get_report_data).")
        return view("get_started", T("view.start"), blocks, subtitle=T(f"onb.{current}" if current else "onb.complete"), hints=hints)

    # ------------------------------------------------------------- history
    def import_chat_history(self, path: Optional[str] = None, batch_size: int = 20) -> dict:
        self._require_setup()
        loaded = 0
        if path:
            p = Path(path).expanduser()
            loaded = self.load_chat_export(p.name, p.read_bytes())
        self.conn.execute("UPDATE history_queue SET status='done' WHERE status='sent'")
        batch = [dict(r) for r in self.conn.execute("SELECT id, conversation, sent_at, text FROM history_queue WHERE status='pending' "
                                                    "ORDER BY sent_at LIMIT ?", (max(1, min(batch_size, 50)),))]
        for r in batch:
            self.conn.execute("UPDATE history_queue SET status='sent' WHERE id=?", (r["id"],))
        self.conn.commit()
        counts = {r["status"]: r["n"] for r in self.conn.execute("SELECT status, COUNT(*) n FROM history_queue GROUP BY status")}
        total = sum(counts.values())
        relevant = total - counts.get("skipped", 0)
        done = counts.get("done", 0) + counts.get("sent", 0)
        b = [block("kv", items=[{"t": "kv.hist_total", "v": total, "fmt": "number"}, {"t": "kv.hist_relevant", "v": relevant, "fmt": "number"},
                                {"t": "kv.hist_done", "v": done, "fmt": "number"}]),
             block("progress", value=round(done / relevant, 2) if relevant else 1.0)]
        text = "\n".join(f"[{r['sent_at'][:10] if r['sent_at'] else '?'} · {r['conversation'] or ''}] {r['text'][:600]}" for r in batch)
        hints = (["Messages from the owner's old chats (oldest first) are below. Extract every business fact and call capture with "
                  "source='history' and date = the message date (for visits/sales use the date the owner meant). Skip anything that is "
                  "not a business fact or is already known. Then call import_chat_history() again for the next batch.\n" + text]
                 if batch else ["All relevant old messages have been processed. Summarise what was added (get_data_status)."])
        if path:
            self._mark_used("history")
        return view("import_chat_history", T("view.history"), b, subtitle=T("sub.history", n=N(len(batch)), left=N(counts.get("pending", 0))),
                    hints=hints)

    def load_chat_export(self, name: str, raw: bytes) -> int:
        """Queue the owner's messages from a Claude / ChatGPT export (.zip or conversations.json). Returns new messages."""
        loaded = 0
        known = self._known_words()
        for source, conv, at, text in _read_export(name, raw):
            h = hashlib.sha256(f"{at}|{text}".encode()).hexdigest()[:24]
            if self.conn.execute("SELECT 1 FROM history_queue WHERE id=?", (int(h[:12], 16),)).fetchone():
                continue
            status = "pending" if _relevant(text, known) else "skipped"
            self.conn.execute("INSERT INTO history_queue(id, source, conversation, sent_at, text, status) VALUES (?,?,?,?,?,?)",
                              (int(h[:12], 16), source, conv, at, text[:4000], status))
            loaded += 1
        return loaded

    def _known_words(self) -> set[str]:
        words = set()
        for r in self.conn.execute("SELECT name FROM v_offerings UNION SELECT name FROM v_staff"):
            words |= {w for w in fold(r["name"]).split() if len(w) >= 4}
        return words


def _relevant(text: str, known: set[str]) -> bool:
    t = fold(text)
    if len(t) < 8:
        return False
    return any(ch.isdigit() for ch in t) or any(w in t for w in BUSINESS_WORDS) or any(w in t for w in known)


def is_chat_export(name: str, raw: bytes) -> bool:
    """A Claude / ChatGPT data export: a .zip holding conversations.json, or that file itself."""
    if name.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                return any(n.endswith("conversations.json") for n in z.namelist())
        except zipfile.BadZipFile:
            return False
    return name.lower().endswith("conversations.json")


def _read_export(name: str, raw: bytes):
    """Yield (source, conversation title, ISO time, text) for each message the user wrote."""
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            inner = next((n for n in z.namelist() if n.endswith("conversations.json")), None)
            if not inner:
                raise ValueError("no conversations.json in this export")
            data = json.load(io.TextIOWrapper(z.open(inner), encoding="utf-8"))
    else:
        data = json.loads(raw.decode("utf-8-sig"))
    for conv in data if isinstance(data, list) else data.get("conversations", []):
        if "chat_messages" in conv:                                   # Claude export
            for m in conv["chat_messages"]:
                if m.get("sender") != "human":
                    continue
                text = m.get("text") or " ".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text")
                if text.strip():
                    yield "claude", conv.get("name"), m.get("created_at") or conv.get("created_at"), text.strip()
        elif "mapping" in conv:                                       # ChatGPT export
            import datetime as dt
            for node in conv["mapping"].values():
                m = node.get("message") or {}
                if (m.get("author") or {}).get("role") != "user":
                    continue
                parts = (m.get("content") or {}).get("parts") or []
                text = " ".join(p for p in parts if isinstance(p, str)).strip()
                if text:
                    ts = m.get("create_time") or conv.get("create_time")
                    at = dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat() if ts else None
                    yield "chatgpt", conv.get("title"), at, text
