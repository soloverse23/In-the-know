# Plan: Hosted connector, data in the owner's own cloud

Status: R1–R5 implemented · R6 code ready, deployment pending · 2026-11-20

## 1. Goal

Owners mostly work on their phone but also use a computer. Today In The Know only runs as a local server (stdio) beside Claude Desktop, so it cannot run on a phone.

This plan turns it into **one hosted connector** that works the same in Claude (web, desktop, iOS, Android) and in ChatGPT. **We store no business data.** Each business's memory is a single SQLite file in the owner's own Google Drive or Microsoft OneDrive.

Decisions taken:

| Question | Decision |
|---|---|
| Storage at launch | Google Drive **and** Microsoft OneDrive (Microsoft also opens the door to Outlook) |
| Where the file lives | A **visible** "In The Know" folder that the owner can see, back up and keep |
| Users per business | Owner only. Team sharing comes later through the provider's own file sharing. |
| Billing | Free beta. No accounts on our side, so the server stays **fully stateless**. |
| Local mode | Kept. The `.mcpb` desktop extension stays as the "fully offline" option. |

What we promise the owner:
- Your data lives in your Drive or OneDrive.
- We keep no copy, no database and no logs of your content.
- You can delete the file or revoke access at any time.

What the promise does **not** cover:
- Tool results still pass through the assistant (Claude or ChatGPT), as with every connector.
- While a request runs, the file is in our server's memory.

---

## 2. Owner experience

**Setup, one time, about 20 seconds:**
1. Add the In The Know connector, either from the directory or by pasting its URL in Claude or ChatGPT.
2. Our sign-in page opens with two buttons: **Continue with Google** and **Continue with Microsoft**.
3. The owner accepts the provider's consent screen. It asks only for access to files this app creates.
4. They are back in the chat. The `get_started` flow begins, and the file `In The Know/<business>.itk` is created in their Drive on the first save.

That is the whole setup: no install, no password and no sync settings. Because the connector belongs to the owner's Claude or ChatGPT account, the phone and the computer are connected at once.

**Daily use:** exactly as today, with quiet context and cards on request.

**Recovery cases, handled in chat and never as errors:**
- **File moved or renamed:** we track it by file id, so it keeps working.
- **File deleted:** Claude says so and offers to restore it from the provider's trash or start fresh. It never silently creates an empty memory.
- **Access revoked:** Claude asks the owner to reconnect, with one tap.
- **Two files found** (for example a manual copy): Claude asks which one is the memory.

---

## 3. Architecture

```
Claude / ChatGPT ──MCP over HTTPS + OAuth──▶  In The Know server (stateless, EU)
                                                 │  per request:
                                                 │  1. decrypt token → provider + refresh token + file id
                                                 │  2. get file (RAM cache, version-checked)
                                                 │  3. run tool on in-memory SQLite
                                                 │  4. if changed → upload new version
                                                 ▼
                                   Google Drive  /  Microsoft OneDrive (owner's account)
```

### 3.1 Storage abstraction (`intheknow/store.py`, new)

```
class Store:                       # one per owner
    def load(self) -> bytes | None          # None = no memory yet
    def save(self, data: bytes, base_version: str) -> str   # returns new version, raises Conflict
    def version(self) -> str                # cheap metadata call
```

The store has three implementations:
- `LocalStore` keeps today's behaviour, for the `.mcpb` desktop extension and the tests.
- `GoogleDriveStore` uses scope `drive.file`. This scope is non-sensitive, so it needs no Google security assessment, and the app sees only files it created.
- `OneDriveStore` uses Microsoft Graph with scope `Files.ReadWrite.AppFolder`. The file lives in `Apps/In The Know`, which the owner can see in OneDrive. It works with both personal and work accounts.

### 3.2 In-memory SQLite

- **Loading:** `sqlite3.Connection.deserialize()` loads the file into memory, and `serialize()` turns it back into bytes for upload. Nothing touches our disk.
- **`ContextEngine`:** today it takes a `db_path` and opens the file itself. It should take an open connection plus a `Store` instead, and one engine is created per owner and request (or taken from the cache).
- **Read-only access:** `connect_readonly(path)` (in `query_data` and custom metrics) needs a version for in-memory databases. It deserializes a copy and attaches the same authorizer and masking, which are unchanged.
- **Transactions:** the per-call transaction (`TxConnection.hold`) already makes each tool call all-or-nothing. A call uploads only after it commits successfully.

### 3.3 Cache, locking and conflicts

- **RAM cache:** keyed by file id, with a 10-minute idle lifetime. It is never written to disk and is cleared on restart.
- **Before a read:** a metadata call checks the version. If the remote version is newer, we reload the file.
- **Concurrency:** writes are serialized per owner with a lock. Before an upload we recheck the version. If someone else wrote first, we reload, replay the tool call once, and otherwise return a clear "try again" message.
- **Deployment:** first a single instance, or several with sticky routing by owner. A shared lock (Redis) only becomes necessary if we scale out.
- **Batching:** we do not upload on every read. Tools marked `READ` never upload.

### 3.4 Auth: our server holds no secrets per owner

- **Authorization server:** the server is its own MCP authorization server, following the MCP auth spec with OAuth 2.1 and PKCE, plus dynamic client registration for Claude and ChatGPT.
- **Stateless client registration:** the registration is itself stateless. The `client_id` is a signed blob of the client's metadata.
- **Tokens we issue:** the access and refresh tokens we give Claude or ChatGPT are **encrypted blobs (JWE)**. Each one carries `{provider, provider_refresh_token, file_id, lang}`.
- **Server key:** the only key the server holds is its own encryption key, kept in the host's secret manager and rotated with key ids.
- **Revocation:** the owner revokes access at Google or Microsoft, and the next call fails cleanly and asks them to reconnect. We cannot list or revoke tokens ourselves, which is the trade-off for holding nothing.

### 3.5 Hosting and privacy

- **Hosting:** an EU region for GDPR, since our owners are in Romania, France and Germany. Cloud Run or Fly.io, both with autoscaling.
- **Logging:** logs hold only the tool name, duration, status and a hashed owner id. Tool arguments and results are never logged, and this is enforced by a test.
- **GDPR role:** we are a processor for the duration of a request only. We need a short privacy page and a data processing agreement template.

---

## 4. Changes to the tools for remote use

Implemented in R4. The design changed from the first draft; see the R4 notes in §6.

| Area | Local (desktop extension) | Hosted |
|---|---|---|
| Files | `add_data(paths=…)` | `upload_files` → private upload page: device upload and Google Drive picker |
| Pasted text | `add_data(content, filename)` | `add_data(content, filename)` for short pastes only |
| Old chats | `import_chat_history(path)` | The export `.zip` is added on the upload page, then `import_chat_history()` works through batches |
| Photos (`import_records`) | Claude reads the image and sends rows | Unchanged |

---

## 5. Working with other connectors

Claude and ChatGPT already orchestrate between connectors. Our job is to make In The Know the place where business facts **land** and the place every other action **reads from first**.

1. **Source-aware capture:** `capture` and `import_records` get `source` (gmail, outlook, calendar, square, shopify, stripe, …) and `external_id`.
   - The same email, event or payment is never stored twice.
   - "Synced from Square" shows in the card's source line.
2. **Server instructions and prompts teach the patterns:**
   - Before writing to or about a customer (Gmail, Outlook, WhatsApp), call `get_person_brief`.
   - When the owner asks about the day, read Calendar and turn today's appointments into visits or tasks.
   - The weekly review pulls the new payments from the payments connector, then calls `get_metrics`.
3. **A new `sync_from` prompt** ("Bring in my latest from Calendar / Square / Outlook") runs a guided pull from whichever connectors the owner has connected. It shows one preview card and saves only when the owner confirms.
4. **Outlook:** we use the assistant's own Microsoft 365 / Outlook connector, as with Gmail. Our consent stays file-only, so we never read mail.
   - If an owner cannot get that connector on their plan, we can add **optional** mail and calendar scopes later through incremental consent.
   - *To verify before launch:* whether the Microsoft 365 connector is available on the plans our owners use, in both Claude and ChatGPT.

---

## 6. Phases

| # | Work | Done when |
|---|---|---|
| **R1** | `Store` interface, `LocalStore`, in-memory SQLite, `ContextEngine` refactor to take a connection | All 66 tests pass with `LocalStore` and in memory. Local `.mcpb` unchanged. |
| **R2** | HTTP transport, stateless OAuth (dynamic client registration, PKCE, encrypted tokens), "Continue with Google / Microsoft" page in 4 languages | MCP Inspector connects. Token round-trip tests pass. |
| **R3** | `GoogleDriveStore` + `OneDriveStore`, RAM cache, version checks, locking, conflict replay, recovery cases from §2 | Contract tests run against both providers' sandboxes. Simulated phone-and-desktop parallel writes never lose a change. |
| **R4** | Remote file import (file picker button, chat export from the store), CSP update | Import of the 3 demo businesses works end to end from Drive. |
| **R5** | Connector integration: `source`/`external_id` dedupe, instructions, `sync_from` prompt | Eval scenarios with Calendar + Gmail + Square pass without duplicates. |
| **R6** | EU deploy, logging guard, privacy page + data processing agreement, Google/Microsoft app verification | Real use on Claude iOS, Android, desktop and ChatGPT. Setup takes under 30 seconds. |

**R2 notes (implemented):**
- **Code:** [`seal.py`](../intheknow/seal.py) (AES-256-GCM sealed tokens with key rotation), [`auth.py`](../intheknow/auth.py) (stateless OAuth server and the sign-in page in 4 languages) and [`hosted.py`](../intheknow/hosted.py) (HTTP app and the per-owner engine cache in RAM).
- **Dev storage:** until R3 the server only runs with `--dev-store DIR`, which keeps one file per owner on the server's disk.
- **Owner's date:** "today" now follows the business's country, or a `timezone` setting, rather than the server clock.
- **No empty files:** no memory file is created before the business is set up.
- **Still open, needs the owner's accounts:**
  - a Google Cloud OAuth client (web app, scope `drive.file`) and a Microsoft Entra app registration (`Files.ReadWrite.AppFolder`);
  - the redirect URIs `<base>/callback/google` and `<base>/callback/microsoft`;
  - a public HTTPS URL (a tunnel is fine) to connect from Claude and MCP Inspector.
- **For R6:** with more than one instance, pass a shared `request_state_security` key to the MCP server. The SDK default is a per-process key.

**R3 notes (implemented):**
- **Code:** [`cloud.py`](../intheknow/cloud.py) holds `GoogleDriveStore`, `OneDriveStore` and `ProviderSession`. The session keeps each owner's access token in RAM, renews it before it expires, renews it again on a 401, and retries once on 429 or 5xx.
- **Where the file lives:**
  - **Google:** `In The Know/memory.itk`, tagged with `appProperties itk=memory`, so it is found again after the owner renames or moves it.
  - **OneDrive:** `Apps/In The Know/memory.itk`.
- **Conflicts:**
  - **OneDrive:** uploads send `If-Match` on the eTag, so Microsoft itself refuses a stale write. The engine then reloads and replays the call.
  - **Google:** Drive v3 has no write precondition. We check the version just before uploading, but two *servers* writing within the same instant could still overwrite each other. On one server this cannot happen, because each owner has exactly one engine and its lock orders their calls (tested). **R6 must keep each owner on one instance** (sticky routing), or add a shared lock.
- **Recovery:**
  - **File in the Drive trash:** Claude tells the owner to restore it. Once restored, the next call works again.
  - **File deleted from OneDrive:** Claude points the owner to the recycle bin.
  - **Access removed:** Claude asks the owner to reconnect.
  - **Several copies found:** the most recently changed one is used. Asking the owner which copy to keep is a later improvement.
- **Reads:** read-only calls cost one metadata request each and never upload (tested).
- **File size:** a typical memory file is 0.4–2.3 MB (the three demo businesses and a real one), so each upload is small.
- **Tests:** the contract tests run against in-memory fakes of the Drive v3 and Graph endpoints we use ([`tests/clouds.py`](../tests/clouds.py)). **Still to do:** one run against real accounts once the Google and Microsoft apps are set up.

**R4 notes (implemented, design changed from §4):**
- **Why the plan changed:** the plan put a Drive picker button *inside* the chat card. That card runs in the assistant's sandboxed iframe, where Google's Picker scripts and popups are unlikely to work. Also, `drive.file` cannot read files the owner drops into our folder; picking a file is what grants access. So `upload_files` now gives the owner a **private upload page on our server**. The card has a button for it, and the link is also in the reply.
- **Upload from the device:**
  - **Formats:** CSV, Excel, notes, or a Claude/ChatGPT export `.zip`.
  - **Phones:** the file chooser also offers Drive, OneDrive and iCloud.
  - **Processing:** files are processed in RAM straight into the owner's memory file.
  - **Results:** the page shows what was added, in the business's language.
- **Choose from Google Drive** (Google Picker), shown only when `ITK_GOOGLE_API_KEY` and `ITK_GOOGLE_APP_ID` are set:
  - Google Sheets are exported as CSV and Docs as text.
  - The file's name and type come from Drive, never from the browser.
- **Link safety:**
  - The ticket is sealed and lasts 30 minutes.
  - Opening the page binds the link to that browser with a cookie. Another browser, or a POST without opening the page, gets "ask Claude for a new link".
  - Limits: 20 files of 25 MB each, 60 MB in total.
- **Tool changes in hosted mode:** `add_data` drops `paths` and keeps only short pasted text, and `import_chat_history` drops `path`. Local desktop mode is unchanged.
- **Engine changes:** files are read from bytes (`source_from_bytes`, `add_files`, `load_chat_export`), and a new `import_uploads` operation takes both data files and chat exports.
- **Not done yet:**
  - **OneDrive picker.** OneDrive owners use the device upload; on phones the file chooser reaches OneDrive.
  - **Picker setup in Google Cloud:**
    1. Enable the *Google Picker API*.
    2. Create an API key restricted to the server's domain (`ITK_GOOGLE_API_KEY`).
    3. Use the project number as `ITK_GOOGLE_APP_ID`.

**R5 notes (implemented):**
- **`external_refs` table:** remembers each item brought in from another app as (source, external_id, kind), plus a fingerprint of what it said and the undo id of what was saved.
- **`capture` items** take `source` (calendar, gmail, outlook, square, shopify, stripe, …) and `external_id`:
  - **Same item, unchanged:** status `known` ("already synced from Calendar").
  - **Same item, changed** (a moved or cancelled appointment): the saved record is undone and the new version saved. This runs inside a savepoint, so if the new version cannot be saved the old one stays.
  - **One email, several facts:** different capture types sharing one external_id work as expected.
  - **On the card:** synced records show a chip with the app's name.
- **`import_records`** with an app source: rows carry `external_id`, rows already synced are skipped, and records keep `_source = sync:<app>`.
- **`capture(dry_run=true)`:** a preview card ("Will be saved") that keeps nothing and uploads nothing. The instructions and the `sync_from` prompt say to preview first and save after the owner agrees.
- **`get_context`:** says when each app was last synced, so the assistant only reads newer items.
- **Instructions:** before writing to a customer or preparing a meeting, call `get_person_brief`.
- **Fixed along the way:**
  - A bare `SAVEPOINT` outside a transaction would start, and on `RELEASE` commit, its own transaction. That would have broken all-or-nothing tool calls and previews. We now open the transaction first.
  - Rows from `import_records` always got `_source = file:<id>`. Synced rows now keep their app source.
- **Tests:** server behaviour is covered by `tests/test_sync.py`. **Not done:** the paid LLM eval (`eval/run_eval.py`) has no Calendar/Gmail/Square scenarios yet. That needs real connector data and an API budget.

**R6 notes (code ready; deployment needs accounts):**
- **Docker image:** non-root, nothing written to disk, health check. Settings come from the environment.
- **Logging ([`logs.py`](../intheknow/logs.py)):**
  - Libraries that would log response bodies or URLs (`sse_starlette`, `httpx2`, `mcp`) are capped at WARNING.
  - A redaction filter masks sealed tokens, bearer tokens and secrets in query strings.
  - Each tool call writes one line: tool, ms, ok, hashed owner.
  - A test runs a full flow at DEBUG and fails if a customer name, phone or token reaches the logs. Checked by switching the cap off: the test caught the customer name.
- **Public pages ([`site.py`](../intheknow/site.py)):** `/` (four languages) and `/privacy`. The policy follows what the code does and includes Google's Limited Use statement. The operator's name and contact come from the environment.
- **Steps, environment variables and smoke test:** see [launch-checklist.md](launch-checklist.md).
- **Beta runs as exactly one instance.**

The order R1 → R2 → R3 is strict. R4 and R5 can run in parallel after R3.

---

## 7. Risks and open points

- **Latency.** The first call of a session downloads the file. A typical business file is under 5 MB, so that is about 0.3–0.8 s, and later calls hit the cache. We measure this in R3, and if needed compress the upload or split out old `raw_files` content.
- **Google app verification.** `drive.file` avoids the heavy audit, but the app still needs brand verification (domain, privacy page) before most users see it without an "unverified app" warning. Start this in R2, because it can take weeks.
- **Microsoft work accounts.** Some organisations block user consent, so an admin has to approve. We detect this and show a clear message the owner can forward to their IT admin.
- **Owner edits the file by hand.** It is a binary SQLite file with a `.itk` extension, so this is unlikely. We validate the file on load, and if it is corrupted we fall back to the provider's version history.
- **Large chat exports.** These are processed in batches, as `import_chat_history` already does. The queue lives in the file, so it survives across requests.
