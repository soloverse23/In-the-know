"""In The Know: one MCP server that is the business memory + analyst inside Claude.

Every tool returns compact English text for the model and a structured view the in-chat
app (ui://intheknow/view.html) renders in the owner's language and brand.

Run:  uv run python -m intheknow.mcp_server --db ~/InTheKnow/business.db
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import time
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, Optional

from mcp.server.apps import Apps, ResourceCsp
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import Icon, ToolAnnotations
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, Field

from . import design, i18n
from .context import SERVER_INSTRUCTIONS, ContextEngine

VERSION = "1.0.0"
UI_URI = "ui://intheknow/view.html"
MARK_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="9" fill="#14161F"/>'
            '<path d="M8.5 20.5c2.6-6.2 12.4-6.2 15 0" stroke="#FAF8F4" stroke-width="2.6" fill="none" stroke-linecap="round"/>'
            '<circle cx="16" cy="12.4" r="3.3" fill="#FF6B4A"/></svg>')
ICON = Icon(src="data:image/svg+xml;base64," + base64.b64encode(MARK_SVG.encode()).decode(), mime_type="image/svg+xml", sizes=["any"])

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

Segment = Literal["due_soon", "lapsed_regulars", "at_risk", "top_spenders", "active", "one_time"]
Lang = Literal["en", "fr", "ro", "de"]
WHERE_DOC = ("Filter: {search: text in any text field or linked name, equals:{field: value}, contains:{field: text}, "
             "min:{field: value}, max:{field: value}, date_from, date_to, empty:[fields], ids:[..], file_id, source: chat|history|photo|file, "
             "fact:{kind?, key?, text?, polarity?} (people only: e.g. who must have parking)}")


HOSTED_NOTE = ("\nFiles: you cannot pass the owner's files to In The Know. When they want to add files, old chats or exports, "
               "call upload_files and give them the link; when they come back, call get_context.")


def ui_html() -> str:
    html = (Path(__file__).parent / "ui" / "view.html").read_text(encoding="utf-8")
    return html.replace("/*I18N*/{}/*END*/", json.dumps(i18n.all_tables(), ensure_ascii=False))


class CaptureItem(BaseModel):
    """One business fact from the conversation. Fill only the fields that apply."""

    type: Literal["transaction", "visit", "sale", "appointment", "customer_note", "customer_info", "offering", "service",
                  "product", "expense", "goal", "policy", "fact", "decision", "staff", "record", "task", "relation"] = Field(description=(
        "transaction|visit|sale|appointment: something sold/done (customer, offering, amount, date, staff, quantity, status). "
        "customer_note: preference/allergy/personal detail (customer, text). customer_info: new customer or phone/fields (customer, phone, fields). "
        "offering|service|product: price or cost of something sold (offering, price, unit_cost, category). "
        "expense: a cost (amount, category, month); rent/salaries/subscriptions that repeat -> recurring='monthly'. "
        "task: a promise, follow-up, meeting, call, delivery or money someone will pay (text, date, time, staff, customer, amount). "
        "fact WITH customer: something about that person (customer, fact_kind, text, key?, polarity?, date = when it was said). "
        "goal|policy|fact without customer: lasting business knowledge (text). decision: something the owner decided (text, expected_outcome, review_on). "
        "relation: how `customer` is linked to `other` (customer, relation, other). "
        "staff: team change (staff, text, date). record: a row of a custom entity such as a pet (entity, fields)."))
    quote: str = Field(description="The owner's exact words for this item")
    customer: Optional[str] = Field(None, description="Customer name as said")
    phone: Optional[str] = Field(None, description="Customer phone, any format")
    offering: Optional[str] = Field(None, description="Service/product name as said (variants and other languages are matched)")
    amount: Optional[str] = Field(None, description="Amount paid or expense amount, e.g. '350 lei', '45,00 €', '12.5'")
    quantity: Optional[float] = Field(None, description="Quantity, for shops")
    date: Optional[str] = Field(None, description="YYYY-MM-DD or 'today'/'yesterday'; default today. For old chats: the date the owner meant")
    time: Optional[str] = Field(None, description="HH:MM")
    staff: Optional[str] = Field(None, description="Team member")
    status: Optional[Literal["done", "no_show", "cancelled"]] = Field(None, description="Appointment outcome")
    text: Optional[str] = Field(None, description="Note / goal / rule / fact / decision / staff-change text, in the owner's language")
    category: Optional[str] = Field(None, description="Expense or product category")
    month: Optional[str] = Field(None, description="Expense month YYYY-MM; default this month")
    price: Optional[str] = Field(None, description="List price of an offering")
    unit_cost: Optional[str] = Field(None, description="Material/goods cost per unit of an offering")
    entity: Optional[str] = Field(None, description="type=record (or a specific transaction table): entity name")
    fields: Optional[dict[str, Any]] = Field(None, description="Extra field values by field name (record type, custom fields)")
    expected_outcome: Optional[str] = Field(None, description="Decision: what should happen")
    review_on: Optional[str] = Field(None, description="Decision: when to check the result (default +30 days)")
    reason: Optional[str] = Field(None, description="Decision: why")
    source: Optional[str] = Field(None, description="Where it came from: omit for the owner's own words; 'history' for old chats; 'photo'; "
                                  "or the app it came from when you read it with another connector: calendar, gmail, outlook, square, shopify, stripe...")
    external_id: Optional[str] = Field(None, description="With an app source: the item's id there (event id, message id, payment id). "
                                       "Saving the same id again is skipped; if the item changed (e.g. moved event) the saved one is replaced. "
                                       "If one app item gives several facts of the same type, add '#2', '#3' to the id")
    recurring: Optional[Literal["monthly"]] = Field(None, description="expense: 'monthly' for rent, salaries, subscriptions; replaces the previous amount of that category from `month`")
    fact_kind: Optional[str] = Field(None, description="fact about a person: preference, dislike, must, must_not, goal, constraint, health, allergy, motivation, communication_style, family, finances, life_event, document, consent, note, or a kind from get_context")
    key: Optional[str] = Field(None, description="fact: the comparable aspect, e.g. 'budget', 'floor', 'area', 'diet', so a later different value is caught as a change")
    polarity: Optional[Literal["likes", "dislikes", "must", "must_not", "neutral"]] = Field(None, description="fact: default comes from the kind")
    sensitivity: Optional[Literal["normal", "private", "special"]] = Field(None, description="fact: raise it for health, money troubles, documents (default from the kind)")
    confidence: Optional[Literal["confirmed", "stated", "inferred"]] = Field(None, description="fact: 'stated' if the person said it but it is not confirmed, 'inferred' if you concluded it")
    valid_until: Optional[str] = Field(None, description="fact: when it stops being true (e.g. 'on a diet until March')")
    relation: Optional[str] = Field(None, description="relation: how `customer` relates to `other`, e.g. husband, wife, mother, son, referred_by, friend, decides_for. customer=Reza relation=husband other=Sara means Reza is Sara's husband")
    other: Optional[str] = Field(None, description="relation: the other person's name")


class ProfileForm(BaseModel):
    name: str = Field(description="Business name")
    vertical: Literal["salon", "clinic", "cafe", "generic"] = Field(description="salon = hair/beauty/nails; clinic = health practice; cafe = café/restaurant/shop; generic = anything else")
    locale: Lang = Field(description="Language")
    currency: Literal["EUR", "RON", "USD", "GBP", "CHF"] = Field(description="Currency")
    country: str = Field(description="Country code, e.g. RO, FR, DE")
    city: str = Field("", description="City")


PROMPTS = {
    "start": {"en": "Start my business day", "fr": "Démarrer ma journée", "ro": "Începe ziua", "de": "Tag starten"},
    "onboarding": {"en": "Set up In The Know", "fr": "Configurer In The Know", "ro": "Configurează In The Know", "de": "In The Know einrichten"},
    "weekly_review": {"en": "Weekly review", "fr": "Bilan de la semaine", "ro": "Analiza săptămânii", "de": "Wochenrückblick"},
    "win_back": {"en": "Win back regulars", "fr": "Faire revenir les fidèles", "ro": "Recâștigă clientele fidele", "de": "Stammkunden zurückgewinnen"},
    "import_history": {"en": "Import my history", "fr": "Importer mon historique", "ro": "Importă istoricul", "de": "Verlauf importieren"},
    "design_dashboard": {"en": "Design my dashboard", "fr": "Créer mon tableau de bord", "ro": "Creează panoul meu", "de": "Mein Dashboard gestalten"},
    "sync_from": {"en": "Bring in my latest from my apps", "fr": "Récupérer le nouveau de mes applis", "ro": "Adu noutățile din aplicațiile mele",
                  "de": "Neues aus meinen Apps holen"},
    "monthly_report": {"en": "Monthly report", "fr": "Rapport mensuel", "ro": "Raport lunar", "de": "Monatsbericht"},
}


def build_server(engine: ContextEngine | Callable[[], ContextEngine], upload_link: Optional[Callable[[], str]] = None,
                 on_call: Optional[Callable[[str, float, bool], None]] = None, **server_kw: Any) -> MCPServer:
    """`engine` is one business (local mode) or a function returning the calling owner's engine (hosted mode).
    `upload_link` (hosted) makes a link to the owner's upload page; local paths are then not offered.
    `on_call(tool, ms, ok)` is told about every tool call (operations logging; never arguments or results).
    `server_kw` is passed to MCPServer (auth settings, auth provider)."""
    current: Callable[[], ContextEngine] = (lambda: engine) if isinstance(engine, ContextEngine) else engine
    hosted = not isinstance(engine, ContextEngine)
    apps = Apps()
    apps.add_html_resource(UI_URI, ui_html(), name="in-the-know", title="In The Know",
                           csp=ResourceCsp(resource_domains=["https://fonts.googleapis.com", "https://fonts.gstatic.com"]),
                           prefers_border=False)
    ui_tools: list = []
    plain_tools: list = []

    def ui(**kw):
        def deco(fn):
            ui_tools.append((fn, kw))
            return fn
        return deco

    def plain(**kw):
        def deco(fn):
            plain_tools.append((fn, kw))
            return fn
        return deco

    def run(name: str, args: dict) -> CallToolResult:
        t0 = time.perf_counter()
        try:
            text, view = current().call_view(name, args)
        except Exception as exc:  # noqa: BLE001 - errors go back to the model as text it can act on
            if on_call:
                on_call(name, (time.perf_counter() - t0) * 1000, False)
            return CallToolResult(content=[TextContent(type="text", text=f"Error: {exc}")], is_error=True)
        if on_call:
            on_call(name, (time.perf_counter() - t0) * 1000, True)
        if view is None:
            return CallToolResult(content=[TextContent(type="text", text=text)])
        return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=view)

    # ---------------------------------------------------------------- onboarding
    @ui(title="Get started", annotations=READ)
    async def get_started(ctx: Context,
                          step_done: Annotated[Optional[Literal["instructions", "review"]], Field(description="Mark a manual step as done")] = None) -> CallToolResult:
        """Onboarding: where the owner is in setup (connect, profile, Project instructions, import, review, first tries) and the next step. Call first when the business is not set up. Shows a native profile form when the client supports it."""
        if not current().is_setup and step_done is None:
            try:
                res = await ctx.elicit("Welcome to In The Know. Tell me about your business.", ProfileForm)
                if res.action == "accept" and res.data:
                    return run("setup_business", res.data.model_dump())
            except Exception:  # noqa: BLE001 - no form support: the assistant asks in chat
                pass
        return run("get_started", {"step_done": step_done})

    @ui(title="Set up the business", annotations=WRITE)
    def setup_business(
        name: Annotated[str, Field(description="Business name")],
        vertical: Annotated[str, Field(description="salon | clinic | cafe | generic (anything else; extend later with change_schema)")],
        locale: Annotated[Lang, Field(description="Owner's language")] = "en",
        currency: Annotated[str, Field(description="EUR, RON, USD, GBP, CHF")] = "EUR",
        country: Annotated[str, Field(description="Country code for phone numbers, e.g. RO, FR, DE")] = "RO",
        city: Annotated[Optional[str], Field(description="City")] = None,
    ) -> CallToolResult:
        """Create the business memory: settings plus a database shaped for the trade (customers, staff, services/products, visits/sales, expenses). Safe to call again to change name/language/currency."""
        return run("setup_business", {"name": name, "vertical": vertical, "locale": locale, "currency": currency, "country": country, "city": city})

    # ------------------------------------------------------------------- read
    @ui(title="Business brief", annotations=READ)
    def get_business_brief() -> CallToolResult:
        """SHOWS the overview card. Call only when the owner asks how the business is doing / for an overview (for background use get_context). Last full month vs the one before (KPIs chosen for this trade), what needs attention, what the owner said in earlier chats, trend, team, prices & margins, decisions due, data quality. Rendered as a card in the owner's language."""
        return run("get_business_brief", {})

    @ui(title="This week", annotations=READ)
    def get_action_list(days: Annotated[int, Field(ge=1, le=60, description="Look-ahead in days")] = 7) -> CallToolResult:
        """SHOWS this week's card: open tasks and promises, regulars due for their usual visit, high-value regulars to win back, customers slipping away, decisions to review, open data questions. Customers contacted recently are left out."""
        return run("get_action_list", {"days": days})

    @ui(title="Review a period", annotations=READ)
    def review_period(start: Annotated[str, Field(description="YYYY-MM-DD")],
                      end: Annotated[str, Field(description="YYYY-MM-DD inclusive; whole months compare to the previous month(s)")]) -> CallToolResult:
        """SHOWS a period review card (call when the owner wants to see a review; for a single number use get_metrics). Performance of any date range vs the previous period: revenue, receipts/visits, customers, new customers, average ticket, costs, net profit, no-shows, by offering and team member, by hour for shops."""
        return run("review_period", {"start": start, "end": end})

    @ui(title="Find customers", annotations=READ)
    def find_customers(segment: Annotated[Segment, Field(description="due_soon | lapsed_regulars | at_risk | top_spenders | active | one_time")],
                       min_visits: Annotated[int, Field(ge=1)] = 3, min_days_since_last_visit: Annotated[int, Field(ge=1)] = 60,
                       limit: Annotated[int, Field(ge=1, le=100)] = 15) -> CallToolResult:
        """Customers in a segment with phone, visits, spend, last visit, rhythm, usual offering and staff."""
        return run("find_customers", {"segment": segment, "min_visits": min_visits, "min_days_since_last_visit": min_days_since_last_visit, "limit": limit})

    @ui(title="Person brief", annotations=READ)
    def get_person_brief(name_or_phone: Annotated[str, Field(description="Full/partial name in any spelling, or phone")],
                         show_sensitive: Annotated[bool, Field(description="Show health/document facts; only when the owner asks for them by name")] = False) -> CallToolResult:
        """Everything about one person across files, spellings and chats: what they like, dislike, must have and will never accept (with when and how it was said), the people around them, open items, what is not known yet, visits, spend, rhythm (due/overdue), usual offering and staff, notes, related records (e.g. pets). Sensitive facts stay masked unless asked. Call before meeting or writing to someone."""
        return run("get_person_brief", {"name_or_phone": name_or_phone, "show_sensitive": show_sensitive})

    @ui(title="Decision help", annotations=READ)
    def prepare_decision_context(topic: Annotated[str, Field(description="The decision in the owner's words")],
                                 offering: Annotated[Optional[str], Field(description="Offering concerned, if any")] = None) -> CallToolResult:
        """Facts for a decision (discount, price, campaign, hiring): unit economics at 0-30% discount with the volume needed to keep profit, how new-customer cohorts came back, current state, relevant notes, goals and past decisions."""
        return run("prepare_decision_context", {"topic": topic, "offering": offering})

    @ui(title="Data & memory", annotations=READ)
    def get_data_status() -> CallToolResult:
        """Imported files (rows new/duplicate/rejected, dates), the business's database tables and recent schema changes (undoable), open data questions with ids."""
        return run("get_data_status", {})

    @ui(title="Show dashboard", annotations=READ)
    def show_view(name: Annotated[str, Field(description="Name of a dashboard created with update_design")]) -> CallToolResult:
        """Render one of the owner's custom dashboards."""
        return run("show_view", {"name": name})

    @plain(title="Search notes", annotations=READ)
    def search_knowledge(query: Annotated[str, Field(description="Topic words")]) -> CallToolResult:
        """Search the owner's notes, goals, rules and facts from files and earlier chats."""
        return run("search_knowledge", {"query": query})

    @plain(title="Query data (SQL)", annotations=READ)
    def query_data(sql: Annotated[str, Field(description="One read-only SQLite SELECT")]) -> CallToolResult:
        """Read-only SQL when no other tool answers. Select only the columns you need and aggregate in SQL (COUNT/SUM/GROUP BY, WHERE, LIMIT); never SELECT * a whole table: at most 25 rows come back. Views: v_transactions(entity, date, time, party_id, party, phone, offering, category, staff, amount, quantity, status, counted, unit_cost, source), v_customers(id, name, phone, visits, total_spend, first_seen, last_visit), v_offerings(id, name, price, unit_cost, category), v_staff(id, name), v_costs(month, category, amount), v_facts(person_id, person, kind, key, value, polarity, sensitivity, confidence, observed_at, current) with sensitive values masked, v_relations(type, a_id, a_name, b_id, b_name). Money in minor units (cents). Use counted=1 for revenue."""
        return run("query_data", {"sql": sql})

    @plain(title="Data for a report or dashboard Artifact", annotations=READ)
    def get_report_data(kind: Annotated[Literal["monthly", "customers"], Field(description="monthly: KPIs, trends, breakdowns; customers: + top 100 customers")] = "monthly",
                        period: Annotated[Literal["last_month", "this_month", "last_30d", "last_90d", "last_12m"], Field()] = "last_month") -> CallToolResult:
        """Exact numbers plus the business's brand tokens, for building a polished Artifact (monthly report, dashboard, campaign plan, poster). Build the Artifact only from this data."""
        return run("get_report_data", {"kind": kind, "period": period})

    # ------------------------------------------------------------------ write
    @ui(title="Save to memory", annotations=WRITE)
    def capture(items: Annotated[list[CaptureItem], Field(min_length=1, description="Every business fact in the owner's latest message")],
                dry_run: Annotated[bool, Field(description="Preview only: show what would be saved, save nothing. Use for anything brought in from another app before the owner agrees")] = False) -> CallToolResult:
        """Save what the owner tells you, in the same turn: visits/sales (who, what, how much, when, with whom), customer notes and phones, prices and costs, expenses, goals, rules, decisions, team changes, custom records. Nothing is stored unless this tool is called, so never say "saved" without its result. Matches names across spellings and languages, never guesses a customer from a partial name, skips duplicates, and returns items needing one detail. Each saved item can be undone."""
        return run("capture", {"items": [i.model_dump(exclude_none=True) for i in items], "dry_run": dry_run})

    async def _with_progress(ctx: Context, name: str, args: dict) -> CallToolResult:
        try:
            await ctx.report_progress(0.1, 1.0, "reading")
        except Exception:  # noqa: BLE001
            pass
        result = run(name, args)
        try:
            await ctx.report_progress(1.0, 1.0, "done")
        except Exception:  # noqa: BLE001
            pass
        return result

    if upload_link is None:
        @ui(title="Import files", annotations=WRITE)
        async def add_data(ctx: Context,
                           paths: Annotated[Optional[list[str]], Field(description="Local files or folders (CSV, XLSX, MD, TXT)")] = None,
                           filename: Annotated[Optional[str], Field(description="Name of an attached/pasted file, with extension")] = None,
                           content: Annotated[Optional[str], Field(description="Full text of an attached/pasted CSV or notes file; never a summary")] = None) -> CallToolResult:
            """Import business files in any layout and language: customer lists, booking/POS exports, price lists, expenses, notes. Recognizes columns (en/fr/ro/de), dates, money formats, first/last names; merges spellings and duplicates; skips rows already imported; adds new fields for unknown columns (undoable)."""
            return await _with_progress(ctx, "add_data", {"paths": paths, "filename": filename, "content": content})
    else:
        @ui(title="Add files", annotations=READ)
        def upload_files() -> CallToolResult:
            """Give the owner a private upload page for their files: customer lists, booking/POS exports, price lists, expenses, notes (CSV, Excel, text), or a Claude/ChatGPT export .zip. Works from phone or computer, including Google Drive. Use this whenever the owner wants to bring in files or their history; show them the link."""
            return run("upload_card", {"url": upload_link()})

        @ui(title="Import pasted data", annotations=WRITE)
        async def add_data(ctx: Context,
                           filename: Annotated[str, Field(description="Name for the data, with extension, e.g. clients.csv or notes.md")],
                           content: Annotated[str, Field(description="Full text of a short CSV or notes the owner pasted or attached; never a summary")]) -> CallToolResult:
            """Import a short table or notes the owner pasted or attached in the chat (same matching as file imports). For real files, or anything longer than about 200 lines, use upload_files instead."""
            return await _with_progress(ctx, "add_data", {"filename": filename, "content": content})

    @ui(title="Save rows from a photo or list", annotations=WRITE)
    def import_records(entity: Annotated[str, Field(description="Entity the rows belong to (see get_data_status), e.g. visit, customer, service, expense")],
                       rows: Annotated[list[dict[str, Any]], Field(description="Rows keyed by field name; refs by name, e.g. {'date':'2026-10-03','customer':'Ana Pop','service':'Tuns','amount':'120'}")],
                       source: Annotated[str, Field(description="Where the rows came from: 'photo:agenda_oct.jpg', 'list', or the app you read them from with another connector (square, shopify, stripe, calendar...)")] = "photo") -> CallToolResult:
        """Save rows you extracted yourself from photos (appointment book pages, receipts, price boards), from lists the owner pasted, or many items read from another app (payments, orders, bookings). For app rows put the item's id in each row as external_id: rows already synced are skipped. Same matching and duplicate checks as file imports."""
        return run("import_records", {"entity": entity, "rows": rows, "source": source})

    @ui(title="Fix column mapping", annotations=WRITE)
    def map_columns(file_id: Annotated[int, Field(description="From add_data / get_data_status")],
                    entity: Annotated[str, Field(description="What one row of the file is")],
                    columns: Annotated[dict[str, str], Field(description="header -> target: a field name, 'ref.name' / 'ref.phone' for linked records, 'name.first'/'name.last', 'new' (create field), 'extra' or 'ignore'")]) -> CallToolResult:
        """Set or correct how a file's columns are understood and re-import it. The layout and header synonyms are remembered for next time."""
        return run("map_columns", {"file_id": file_id, "entity": entity, "columns": columns})

    @ui(title="Change the database", annotations=WRITE)
    def change_schema(action: Annotated[Literal["create_entity", "add_field", "rename_field", "remove_field", "change_field", "relabel_entity", "remove_entity",
                                                "add_fact_kind", "add_relation_type"], Field()],
                      entity: Annotated[Optional[str], Field(description="The table (see get_data_status)")] = None,
                      spec: Annotated[Optional[dict[str, Any]], Field(description="create_entity: {name, role: party|staff|offering|transaction|cost|resource|custom, label:{en,fr,ro,de:[singular,plural]}, fields:[{name, type: text|number|money|date|time|month|phone|email|enum|bool|ref, ref?, role_attr?, values?, synonyms?, sensitive?}]}. add_fact_kind: {name, label:{en,fr,ro,de}, polarity?, sensitivity?: normal|private|special, synonyms?}. add_relation_type: {name, label:{en,fr,ro,de:[forward, inverse]}, symmetric?, synonyms?, inverse_synonyms?}")] = None,
                      field: Annotated[Optional[Any], Field(description="add_field: {name, type, label?, synonyms?, ref?, values?}; other actions: the field name")] = None,
                      new_name: Annotated[Optional[str], Field(description="rename_field: new field name")] = None,
                      label: Annotated[Optional[dict[str, Any]], Field(description="rename_field / relabel_entity: {en,fr,ro,de: label or [singular, plural]}")] = None,
                      new_type: Annotated[Optional[str], Field(description="change_field: new type; 'ref' turns a text column into links to another table")] = None,
                      ref: Annotated[Optional[str], Field(description="change_field to ref: the table to link to (e.g. product)")] = None,
                      confirm: Annotated[bool, Field(description="Required to apply removals/conversions of existing data, after the owner agreed to the preview")] = False) -> CallToolResult:
        """Change the business's tables: create a table or field, rename a field or table label, remove a field or table, or change a field's type (text -> number/date/money, or text -> link to another table such as products, matching or creating the records). Also add a new kind of fact about people (add_fact_kind) or a new kind of link between people (add_relation_type). Changes touching existing data first return a preview; repeat with confirm=true after the owner agrees. Every change can be undone."""
        return run("change_schema", {"action": action, "entity": entity, "spec": spec, "field": field, "new_name": new_name,
                                     "label": label, "new_type": new_type, "ref": ref, "confirm": confirm})

    @plain(title="Find records", annotations=READ)
    def find_records(entity: Annotated[str, Field(description="Table name (fields are listed in get_context), e.g. viewing, customer, sale")],
                     where: Annotated[Optional[dict[str, Any]], Field(description=WHERE_DOC)] = None,
                     fields: Annotated[Optional[list[str]], Field(description="Only the fields the question needs")] = None,
                     sort: Annotated[Optional[str], Field(description="Field to sort by; '-field' for descending, e.g. '-date'")] = None,
                     limit: Annotated[int, Field(ge=1, le=50)] = 10,
                     count_by: Annotated[Optional[str], Field(description="Instead of rows, count matches per value of this field")] = None) -> CallToolResult:
        """Answer a question about any table by filtering on the server: returns only the matching rows and only the fields asked for (links by name), or counts per value. Use this instead of reading whole tables. Draws no card; answer in your own words."""
        return run("find_records", {"entity": entity, "where": where, "fields": fields, "sort": sort, "limit": limit, "count_by": count_by})

    @ui(title="Browse records", annotations=READ)
    def browse_records(entity: Annotated[str, Field(description="Table name, e.g. customer, sale, supplier")],
                       where: Annotated[Optional[dict[str, Any]], Field(description=WHERE_DOC)] = None,
                       fields: Annotated[Optional[list[str]], Field(description="Only the columns the owner needs to see")] = None,
                       sort: Annotated[Optional[str], Field(description="Field to sort by; '-field' for descending")] = None,
                       limit: Annotated[int, Field(ge=1, le=200)] = 30) -> CallToolResult:
        """SHOWS rows as a card. Use when the owner wants to see specific records, or to look at rows (with ids) before cleaning. Show only what was asked: filter to those rows (e.g. where={ids:[...]} from find_records) and pass only the needed fields. Links are shown by name."""
        return run("browse_records", {"entity": entity, "where": where, "fields": fields, "sort": sort, "limit": limit})

    @ui(title="Edit records", annotations=DESTRUCTIVE)
    def edit_records(entity: Annotated[str, Field(description="Table name")],
                     action: Annotated[Literal["update", "delete", "merge", "dedupe", "forget_person", "export_person"], Field(description="update rows, delete rows, merge two records of the same thing, dedupe the whole table, erase everything about one person (GDPR), or export everything held about one person")],
                     where: Annotated[Optional[dict[str, Any]], Field(description="update/delete filter: {ids:[..], file_id, source, equals:{field: value}, contains:{field: text}, empty:[fields], date_from, date_to}")] = None,
                     set: Annotated[Optional[dict[str, Any]], Field(description="update: {field: new value} (links by name, money/dates in any format) or {field: {from_field: other_field}} to copy/convert per row")] = None,
                     keep: Annotated[Optional[str], Field(description="merge: id or name of the record to keep")] = None,
                     duplicate: Annotated[Optional[str], Field(description="merge: id or name of the duplicate (its links move to `keep`)")] = None,
                     confirm: Annotated[bool, Field(description="Apply deletes, merges, dedupes, forget_person and updates of >20 rows; first call without it to get a preview for the owner")] = False,
                     person: Annotated[Optional[str], Field(description="forget_person / export_person: name, phone or id")] = None,
                     include_sensitive: Annotated[bool, Field(description="export_person: include health/document facts (default true: it is their own data)")] = True) -> CallToolResult:
        """Clean the data: fix values, fill or copy fields, delete wrong rows (e.g. a bad import: where={file_id}), merge duplicate records of any table (all links, facts and relations are moved), or remove all duplicates of a table. Also erase a person completely (forget_person: their record, facts, links, tasks and mentions in notes and files; sales stay without their name) or export everything held about them (export_person). Safe by design: previews before bulk/destructive changes, one Undo for each change."""
        return run("edit_records", {"entity": entity, "action": action, "where": where, "set": set, "keep": keep,
                                    "duplicate": duplicate, "confirm": confirm, "person": person, "include_sensitive": include_sensitive})

    @ui(title="Design", annotations=WRITE)
    def update_design(patch: Annotated[dict[str, Any], Field(description="Design patch. Allowed: " + json.dumps(design.capabilities()))],
                      note: Annotated[Optional[str], Field(description="What the owner asked for")] = None) -> CallToolResult:
        """Change how In The Know looks and what it shows, from the owner's words: brand colors, font, corners, density, light/dark, logo, order and KPIs of the brief, and custom dashboards (kpis / charts / tables). Versioned; undo_change('design') reverts."""
        return run("update_design", {"patch": patch, "note": note})

    @ui(title="New metric", annotations=WRITE)
    def define_metric(name: Annotated[str, Field(description="lowercase_id")], label: Annotated[dict[str, str], Field(description="{en,fr,ro,de}")],
                      sql: Annotated[str, Field(description="Read-only SELECT returning one number for :start and :end (ISO dates) over the role views")],
                      format: Annotated[Literal["number", "money", "percent"], Field()] = "number") -> CallToolResult:
        """Define a business-specific KPI (e.g. share of keratin in revenue, average items per receipt) usable in the brief and dashboards."""
        return run("define_metric", {"name": name, "label": label, "sql": sql, "format": format})

    @ui(title="Merge customers", annotations=DESTRUCTIVE)
    def merge_customers(keep: Annotated[str, Field(description="Name or phone of the record to keep")],
                        duplicate: Annotated[str, Field(description="Name or phone of the duplicate")]) -> CallToolResult:
        """Merge two records of the same person (all history, phones, notes, spellings). Only after the owner confirms."""
        return run("merge_customers", {"keep": keep, "duplicate": duplicate})

    @plain(title="Answer data questions", annotations=WRITE)
    def resolve_question(resolution: Annotated[Literal["confirm", "dismiss"], Field()],
                         question_ids: Annotated[Optional[list[int]], Field(description="Several question ids at once")] = None,
                         question_id: Annotated[Optional[int], Field()] = None,
                         all_open: Annotated[bool, Field(description="Apply to every open question")] = False,
                         note: Annotated[Optional[str], Field()] = None) -> CallToolResult:
        """Close open data questions (from get_data_status) after asking the owner, in ONE call (question_ids or all_open). No card."""
        return run("resolve_question", {"resolution": resolution, "question_ids": question_ids, "question_id": question_id,
                                        "all_open": all_open, "note": note})

    @plain(title="Business context (quiet)", annotations=READ)
    def get_context() -> CallToolResult:
        """Call quietly at the start of every conversation: the business in a few lines (key numbers, what changed since last time, what needs attention, open tasks, recurring costs, what the owner said before, goals, tables). Draws no card."""
        return run("get_context", {})

    @plain(title="Quick numbers", annotations=READ)
    def get_metrics(metrics: Annotated[Optional[list[str]], Field(description="revenue, transactions, receipts, unique_customers, new_customers, avg_ticket, cost_of_goods, gross_profit, expenses, net_profit, no_show_rate, items_sold")] = None,
                    period: Annotated[Literal["last_month", "this_month", "last_30d", "last_90d", "last_12m"], Field()] = "last_month",
                    start: Annotated[Optional[str], Field(description="YYYY-MM-DD (overrides period)")] = None,
                    end: Annotated[Optional[str], Field(description="YYYY-MM-DD inclusive")] = None,
                    by: Annotated[Optional[Literal["offering", "staff", "category", "hour"]], Field()] = None) -> CallToolResult:
        """Exact numbers to answer a quick question in text, with the previous period for comparison. Draws no card; use review_period only when the owner wants to see a review."""
        return run("get_metrics", {"metrics": metrics, "period": period, "start": start, "end": end, "by": by})

    @plain(title="Complete a task", annotations=WRITE)
    def complete_task(task_id: Annotated[int, Field(description="Task id from get_context / get_action_list")],
                      undo: Annotated[bool, Field(description="Reopen instead")] = False) -> CallToolResult:
        """Mark a task (promise, follow-up, meeting) as done. No card."""
        return run("complete_task", {"task_id": task_id, "undo": undo})

    @plain(title="Log a contact", annotations=WRITE)
    def mark_contacted(customer: Annotated[str, Field(description="Name or phone")], channel: Annotated[Optional[str], Field()] = None,
                       note: Annotated[Optional[str], Field()] = None) -> CallToolResult:
        """Record that the owner contacted a customer (so reminders don't repeat)."""
        return run("mark_contacted", {"customer": customer, "channel": channel, "note": note})

    @plain(title="Undo", annotations=DESTRUCTIVE)
    def undo_change(change_id: Annotated[str, Field(description="An undo id from a card: schema change number, 'design', 'rec:<entity>:<id>', 'kn:<id>' or 'dec:<id>'")]) -> CallToolResult:
        """Undo something saved from chat, a schema change or the last design change."""
        return run("undo_change", {"change_id": change_id})

    if upload_link is None:
        @ui(title="Import old chats", annotations=WRITE)
        def import_chat_history(path: Annotated[Optional[str], Field(description="Claude or ChatGPT export (.zip or conversations.json); omit to get the next batch")] = None,
                                batch_size: Annotated[int, Field(ge=1, le=50)] = 20) -> CallToolResult:
            """Bring business facts from the owner's old Claude/ChatGPT conversations into memory: loads the export, then returns batches of the owner's messages for you to capture (source='history')."""
            return run("import_chat_history", {"path": path, "batch_size": batch_size})
    else:
        @ui(title="Go through old chats", annotations=WRITE)
        def import_chat_history(batch_size: Annotated[int, Field(ge=1, le=50)] = 20) -> CallToolResult:
            """Work through the owner's old Claude/ChatGPT messages in batches and capture the business facts (source='history'). The owner first adds their export .zip on the upload page (upload_files)."""
            return run("import_chat_history", {"batch_size": batch_size})

    for fn, kw in ui_tools:
        apps.tool(resource_uri=UI_URI, meta={"ui/resourceUri": UI_URI}, **kw)(fn)

    mcp = MCPServer("in-the-know", title="In The Know", description="The memory and analyst of your business, inside Claude",
                    instructions=SERVER_INSTRUCTIONS + (HOSTED_NOTE if upload_link else ""), version=VERSION, icons=[ICON], extensions=[apps], **server_kw)
    for fn, kw in plain_tools:
        mcp.tool(**kw)(fn)

    # ------------------------------------------------------------- resources
    @mcp.resource("intheknow://brief", name="business-brief", title="Business brief", mime_type="text/markdown",
                  description="Current business brief (attach to a chat for context)")
    def brief_resource() -> str:
        return current().call("get_business_brief", {})

    @mcp.resource("intheknow://this-week", name="this-week", title="This week", mime_type="text/markdown",
                  description="Reminders and customers to contact this week")
    def week_resource() -> str:
        return current().call("get_action_list", {})

    # --------------------------------------------------------------- prompts
    # Prompt titles are fixed when the server starts: the business language locally, English when hosted
    # (one server serves every owner). The reply language inside each prompt follows the calling owner.
    lang = "en" if hosted else (engine.lang if engine.lang in i18n.LANGS else "en")

    class _Reply:
        def __str__(self) -> str:
            lg = current().lang
            return {"en": "Answer in English.", "fr": "Réponds en français.", "ro": "Răspunde în română.",
                    "de": "Antworte auf Deutsch."}.get(lg, "Answer in English.")

    reply = _Reply()

    @mcp.prompt(name="start", title=PROMPTS["start"][lang])
    def start() -> str:
        """Start a business conversation with In The Know rules loaded."""
        return ("This conversation is about my business; In The Know is its memory. Call get_business_brief now and give me the 3 things "
                "that matter today. From now on, whenever I mention a business fact (visit, sale, customer detail, price, expense, goal, "
                f"rule, decision, team change), save it with capture in the same turn. {reply}")

    @mcp.prompt(name="onboarding", title=PROMPTS["onboarding"][lang])
    def onboarding() -> str:
        """Guided setup from an empty memory."""
        return f"Set up In The Know for my business: call get_started and guide me step by step until everything is ready. {reply}"

    @mcp.prompt(name="weekly_review", title=PROMPTS["weekly_review"][lang])
    def weekly_review() -> str:
        """Weekly review with 3 concrete actions."""
        return f"Weekly review: get_business_brief, then get_action_list. End with the 3 most important actions this week, with names and numbers. {reply}"

    @mcp.prompt(name="win_back", title=PROMPTS["win_back"][lang])
    def win_back() -> str:
        """Personal messages for lost regulars."""
        return (f"Find my lost regulars (find_customers lapsed_regulars). For the top 5, read get_person_brief and write each a short, warm, "
                f"personal message in my voice; if their usual team member left, introduce someone else. {reply}")

    @mcp.prompt(name="import_history", title=PROMPTS["import_history"][lang])
    def import_history(path: str = "") -> str:
        """Bring facts from old Claude/ChatGPT chats into memory."""
        if upload_link is not None:
            where = "Tell me how to export my Claude or ChatGPT history, then give me the upload page (upload_files) to add the .zip."
        else:
            where = f"My export is at {path}." if path else "Tell me how to export my Claude or ChatGPT history, then I'll give you the path."
        return f"{where} Use import_chat_history and capture (source='history') until all batches are done. {reply}"

    @mcp.prompt(name="design_dashboard", title=PROMPTS["design_dashboard"][lang])
    def design_dashboard(idea: str = "") -> str:
        """Design a custom dashboard from a description."""
        return (f"Design a dashboard for my business{': ' + idea if idea else ''}. Propose it in one sentence, then create it with update_design "
                f"(views) and show it with show_view. {reply}")

    @mcp.prompt(name="sync_from", title=PROMPTS["sync_from"][lang])
    def sync_from(apps: str = "") -> str:
        """Bring new appointments, payments, orders and promises from the owner's other connectors into memory."""
        which = f"from {apps}" if apps else "from every other connector I have connected (calendar, email, payments, shop)"
        return ("Bring my latest business facts into In The Know " + which + ". First call get_context: it says when each app was last "
                "synced; read only what is newer (if never, the last 30 days). Calendar: appointments that happened -> transaction "
                "(status done/no_show/cancelled), future ones and meetings -> task. Email: promises, bookings, money owed, customer details -> "
                "task / customer_info / fact. Payments or orders -> transaction (several at once: import_records with external_id per row). "
                "Always set source to the app and external_id to the item's id. First call capture with dry_run=true and give me a "
                f"two-line summary; save only after I say yes. {reply}")

    @mcp.prompt(name="monthly_report", title=PROMPTS["monthly_report"][lang])
    def monthly_report() -> str:
        """A polished monthly report Artifact."""
        return (f"Create a beautiful monthly report Artifact for my business: call get_report_data and build it only from that data, "
                f"with my brand colors, charts and 3 recommendations. {reply}")

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="In The Know MCP server")
    parser.add_argument("--db", default="~/InTheKnow/business.db", help="SQLite file for this business (created empty if missing)")
    parser.add_argument("--today", default=None, help="Override today's date (YYYY-MM-DD), for demos and tests")
    args = parser.parse_args()
    db = Path(args.db).expanduser()
    db.parent.mkdir(parents=True, exist_ok=True)
    today = dt.date.fromisoformat(args.today) if args.today else None
    build_server(ContextEngine(db, today)).run()


if __name__ == "__main__":
    main()
