# In The Know

The memory and analyst of a small business, inside Claude. It is one MCP server with an in-chat UI (MCP Apps), in English, French, Romanian and German.

- **Dynamic database per trade:** each table is bound to a fixed role (party, staff, offering, transaction, cost, resource, custom), so metrics and UI work for any business.
  - Packs exist for salon, clinic, café and generic.
  - Claude can add tables and fields from chat (`change_schema`). Every change is validated and can be undone.
  - Unknown file columns become new fields automatically.
- **Everything flows in from chat:**
  - Files from any export, in any column layout or language (`add_data`).
  - Photos of notebooks and receipts (`import_records`).
  - Old Claude or ChatGPT chats (`import_chat_history`).
  - Anything the owner says (`capture`).
  - Names are matched across spellings and duplicates are never counted twice. A partial name is never guessed: Claude asks instead.
- **Quiet by default:** `get_context` gives Claude the business in a few lines with no card, including what changed since the last check, open tasks and recurring costs.
  - Cards appear only when the owner asks to see something. Quick questions are answered in text (`get_metrics`).
  - Housekeeping tools (undo, questions, contacts, tasks) draw no card.
  - A save is confirmed in one compact line.
- **Understands what it hears:**
  - Promises, meetings and money due become dated tasks with a Done button.
  - Rent and salaries become recurring monthly costs that are included in profit.
  - New-customer questions are grouped, and skipped when there is no customer list to compare against.
- **Exact numbers:** every metric is computed in `metrics.py`, not by the model. Tool text goes to the model in English; the cards render in the owner's language.
- **Answers without dumping tables:** `find_records` filters, sorts and counts any table on the server and returns only the matching rows and the fields asked for, with no card.
  - `query_data` returns at most 25 rows and asks Claude to aggregate instead.
  - When the owner wants to see the result, `browse_records` shows only those rows and columns.
- **Cleaning by chat:** `browse_records` to look at rows; `edit_records` to update, delete, merge or dedupe; `change_schema` to rename, remove or convert a field (for example a text column into a link to products) or to remove a table.
  - Bulk and destructive changes show a preview and need the owner's confirmation.
  - Every change is snapshotted, so Undo restores it exactly.
  - A failed tool call changes nothing.
- **Design by chat:** brand colours, font, corners, density, logo, brief layout and custom dashboards (`update_design`). It is versioned and can be undone, and no arbitrary HTML is ever run.
- **First run from an empty memory:** `get_started` walks through profile, Claude Project instructions, import, review and three practice steps. It uses a native form when the client supports one.

```
intheknow/
  normalize.py   en/fr/ro/de text, money (minor units), dates, phones (E.164), names
  schema.py      dynamic schema engine: roles, specs, apply/undo, role views
  verticals.py   salon / clinic / cafe / generic packs (synonyms in 4 languages)
  mapping.py     column -> field mapping, plausibility checks, layout memory
  ingest.py      files / records -> resolution (people, offerings, staff) -> tables, dedupe
  metrics.py     every business metric, over role views; custom metrics
  design.py      brand tokens, brief layout, custom dashboards (validated patches)
  views.py       language-neutral view model + English text for the model
  i18n.py        translations + locale formatting; ui/i18n/{en,fr,ro,de}.json
  context.py     read tools (brief, actions, period, customers, decisions, data, views, reports)
  operations.py  write tools (setup, import, capture, schema, design, merge, undo...)
  editing.py     cleaning: browse rows, update/delete/merge/dedupe, field & table changes (snapshots for undo)
  people.py      people layer: facts (sourced, superseded not overwritten), relations, sensitivity, consent, forget/export
  onboarding.py  first-run checklist, Project instructions, old-chat import
  store.py       where the memory file lives (local file today; owner's Drive/OneDrive next); the engine works in memory
  mcp_server.py  MCP server: 31 tools, 7 prompts, resources, icon, MCP Apps UI
  ui/view.html   the in-chat app (renderer, brand, animations, actions)
data/synth.py    three messy demo businesses + ground truth (Bucharest salon, Lyon physio, Berlin café)
packaging/       Claude Desktop extension (.mcpb) build
docs/            brand.md + screenshots of every card
```

## Run and test

```bash
uv sync
uv run python data/synth.py            # regenerate demo data + ground truth
uv run pytest -q                       # 72 tests: normalizers, ground truth x3, tool flows, cleaning, smart behaviour, people layer, storage
uv run python eval/run_eval.py --dry-run
```

## Install in Claude Desktop

**One click:** `uv run python packaging/build.py` creates `dist/in-the-know.mcpb`. Double-click it to install. Claude asks where to keep the memory (default `~/InTheKnow`).

**Manual:** add this to `claude_desktop_config.json` and restart Claude:

```json
{"mcpServers": {"in-the-know": {"command": "/Users/YOU/.local/bin/uv",
  "args": ["--directory", "/path/to/In-The-Know", "run", "python", "-m", "intheknow.mcp_server",
           "--db", "/Users/YOU/InTheKnow/business.db"]}}}
```

Then open a new chat and choose **+ → In The Know → Set up In The Know**, or just say hello.
