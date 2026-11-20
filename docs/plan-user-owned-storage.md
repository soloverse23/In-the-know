# Plan: Hosted connector, data in the owner's own cloud

Status: R1–R3 implemented · R4–R6 proposed · 2026-11-20

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

| Area | Today | Remote |
|---|---|---|
| `add_data(paths=…)` | Reads local paths | `content` + `filename` (already supported). We add a **"Choose from Drive / OneDrive" button** in the card, using the Google Picker or OneDrive file picker. Picking a file grants access to that one file. |
| `import_chat_history(path)` | Reads a local export `.zip` | The owner uploads the export to the In The Know folder, or picks it with the file picker. The server reads it from the store. |
| Photos (`import_records`) | Claude reads the image and sends rows | Unchanged |
| `--db`, `--today` | CLI flags | Unchanged for local mode. Remote mode gets an `--http` entry point. |
| UI resource CSP | Fonts only | Add the picker domains (Google and Microsoft) |

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

The order R1 → R2 → R3 is strict. R4 and R5 can run in parallel after R3.

---

## 7. Risks and open points

- **Latency.** The first call of a session downloads the file. A typical business file is under 5 MB, so that is about 0.3–0.8 s, and later calls hit the cache. We measure this in R3, and if needed compress the upload or split out old `raw_files` content.
- **Google app verification.** `drive.file` avoids the heavy audit, but the app still needs brand verification (domain, privacy page) before most users see it without an "unverified app" warning. Start this in R2, because it can take weeks.
- **Microsoft work accounts.** Some organisations block user consent, so an admin has to approve. We detect this and show a clear message the owner can forward to their IT admin.
- **Owner edits the file by hand.** It is a binary SQLite file with a `.itk` extension, so this is unlikely. We validate the file on load, and if it is corrupted we fall back to the provider's version history.
- **Large chat exports.** These are processed in batches, as `import_chat_history` already does. The queue lives in the file, so it survives across requests.
