# Launch checklist: hosted connector (R6)

Status: code ready · deploy script ready (Cloud Run) · 2026-11-20

The server is one Docker image (`Dockerfile`). It keeps no database and writes nothing to disk. Owners' memories live in their own Google Drive or OneDrive.

---

## 1. Rules for running it

- **Run exactly one instance.**
  - **Why:** each owner's engine, and the lock that orders their writes, lives in that instance's RAM. Google Drive has no write precondition, so two instances writing for the same owner at the same moment could overwrite each other.
  - **Also in RAM:** the used-sign-in-code list and the upload-link browser binding.
  - **Scaling later:** route each owner to a fixed instance (for example Fly's `fly-replay` keyed on the owner hash), or add a shared lock (Redis). Until then one instance with 1–2 vCPU and 1 GB RAM is plenty for the beta.
- **Host in an EU region** (owners in RO, FR, DE).
- **HTTPS only.** The public URL must be the one in `ITK_BASE_URL`.
- **No volume.** A restart only drops the RAM cache. Owners are not signed out, because their tokens are sealed with `ITK_SEAL_KEYS`.

## 2. Environment variables

| Variable | Required | What |
|---|---|---|
| `ITK_BASE_URL` | yes | Public https URL, e.g. `https://app.intheknow.example` |
| `ITK_SEAL_KEYS` | yes | `k1:<key>`. Generate with `python -m intheknow.seal`. **Secret.** To rotate, put the new key first: `k2:<new>,k1:<old>`. |
| `ITK_GOOGLE_CLIENT_ID` / `_SECRET` | yes (or Microsoft) | Google OAuth web client |
| `ITK_MICROSOFT_CLIENT_ID` / `_SECRET` | optional | Entra app registration |
| `ITK_MICROSOFT_TENANT` | optional | Default `common` (personal + work accounts) |
| `ITK_GOOGLE_API_KEY` / `ITK_GOOGLE_APP_ID` | optional | Enables "Choose from Google Drive" on the upload page |
| `ITK_OPERATOR`, `ITK_CONTACT`, `ITK_HOSTING`, `ITK_POLICY_DATE` | yes for launch | Shown on `/privacy` and `/` |
| `ITK_LOG_LEVEL` | optional | Default `INFO`. Libraries that could log content stay capped at WARNING. |
| `ITK_BILLING` | optional | `on` turns on plans (see section 3b). Off: everything stays free, as in the beta. |
| `ITK_PADDLE_ENV` | with billing | `sandbox` (default) or `production` |
| `ITK_PADDLE_CLIENT_TOKEN` | with billing | Public token for Paddle.js (starts `test_` in sandbox, `live_` in production) |
| `ITK_PADDLE_API_KEY` / `ITK_PADDLE_WEBHOOK_SECRET` | with billing | **Secrets.** Kept in Secret Manager by `deploy/cloudrun.sh` |
| `ITK_PADDLE_PRICE_MONTHLY` / `_YEARLY` | with billing | Paddle price ids (`pri_…`) **with** the 14-day trial |
| `ITK_PADDLE_PRICE_MONTHLY_NOTRIAL` / `_YEARLY_NOTRIAL` | optional | Same prices **without** a trial, used when someone already had their trial once. If missing, the trial prices are used. |
| `ITK_FIRESTORE_PROJECT` | CLI only | Project for `python -m intheknow.billing codes` when run on your computer |
| `PORT` | set by host | Default 8080 in the image |

**Secrets** (seal keys, client secrets) go into the host's secret store, never into the repo or the image.

## 0. For now: personal mode (no accounts)

Decided on 2026-11-20: use In The Know right away, without the Google sign-in, billing or a domain.

| | |
|---|---|
| **Where** | On the owner's Mac. ngrok's static domain makes it reachable from Claude on phone and desktop. |
| **Run** | `./deploy/personal.sh`: starts the server and ngrok, keeps the Mac awake, prints the address. Ctrl+C stops it. |
| **Access** | `https://<ngrok-domain>/<ITK_PERSONAL_KEY>/mcp`. The random key, created on the first run and kept in `.env`, is the only access control. Anything without it gets 404. There are no OAuth endpoints. |
| **Data** | `~/InTheKnow/business.db` (`ITK_DB` to change it). This is the same file as the desktop extension, so phone and desktop share one memory. |
| **Uploads** | Work too. Links are sealed with a key held only in RAM. |
| **Privacy** | ngrok runs with `--inspect=false`, and server logs hold no content. |
| **Limits** | Works only while the Mac is on and the window is open. One owner. Whoever has the address has the memory. |

Everything below (Cloud Run, Google and Microsoft sign-in, verification) is for when it should run for other owners.

## 2b. Decisions for the beta (2026-11-20)

| Decision | Choice |
|---|---|
| Hosting | **Google Cloud Run**, `europe-west1` (Belgium), in the same Google Cloud project as the sign-in client |
| Instances | min 1 and max 1 |
| Address | Cloud Run's own address `https://intheknow-<project-number>.europe-west1.run.app` for now. Google brand verification waits for an owned domain. |
| Operator on `/privacy` | Decided later (`ITK_OPERATOR`, `ITK_CONTACT` in `.env`). Must be filled before verification. |
| Microsoft | After the Google beta works end to end |

**Deploy:** run [`deploy/cloudrun.sh`](../deploy/cloudrun.sh). It is safe to re-run. It:
1. enables the APIs;
2. keeps the secrets in Secret Manager (the production seal key is generated once and never replaced);
3. builds the image and deploys with min = max = 1 instance;
4. checks `/health`;
5. prints the redirect URI and the connector address.

During a deploy, Cloud Run briefly runs the old and the new revision side by side. The one-instance rule can be broken for a few seconds, so deploy when few owners are active.

## 3. Google

1. **Domain.** Verify the domain of `ITK_BASE_URL` in Google Search Console with the same Google account that owns the Cloud project.
2. **Branding** (Google Auth Platform):
   - App name `In The Know`, logo, support email.
   - Home page `https://<domain>/`, privacy policy `https://<domain>/privacy`.
   - Authorized domain `<domain>`.
3. **Client.** Add the redirect URI `https://<domain>/callback/google` to the web client. Keep the ngrok one only while testing.
4. **Scopes:** `openid`, `email`, `drive.file`. All three are non-sensitive, so no security assessment is needed.
5. **Verification.** Publish the app (Testing → In production) and submit for **brand verification**. Until it is approved, users see "unverified app", and in Testing mode only listed test users can sign in. Their sign-in also expires after 7 days.
6. **Optional picker:**
   - Enable the *Google Picker API*.
   - Create an API key restricted to *HTTP referrers* `https://<domain>/*` and to the Picker API.
   - Set `ITK_GOOGLE_API_KEY` to that key and `ITK_GOOGLE_APP_ID` to the project number.

## 3b. Billing (Paddle)

**Plan:** add a card → 14 days free → €4.99 a month or €49 a year (VAT included). A promo code gives 3 months free with no card. With no active plan the memory is read-only; the data stays in the owner's Drive.

**What is stored:** a small Firestore database in the same project and EU region: per owner a hash, plan status, end date, "trial used", Paddle ids and the code used; and the hashes of promo codes. No email, no business data. (The privacy policy says exactly this.)

**Why Paddle:** it is the seller of record. Paddle collects the card, charges the VAT of the buyer's country and issues the invoices, so there is no EU VAT (OSS) registration and no e-Factura per sale. You invoice Paddle once for the payout. Fees are about 5% + $0.50 per payment.

### Set up (do it in the sandbox first)
1. Create a Paddle account (it asks for your Romanian company details; approval can take days, so start early). Use **sandbox** while testing: `sandbox-vendors.paddle.com`.
2. **Catalog → Products:** one product "In The Know". Add **four prices**, all in **EUR, tax inclusive** (VAT included in the shown price):
   | Price | Amount | Billing | Trial |
   |---|---|---|---|
   | Monthly | 4.99 | every month | 14 days |
   | Yearly | 49.00 | every year | 14 days |
   | Monthly, no trial | 4.99 | every month | none |
   | Yearly, no trial | 49.00 | every year | none |

   The no-trial pair is only for people who already had their trial once. Copy each `pri_…` id into `.env`.
3. **Developer tools → Authentication:** create a **client-side token** (`ITK_PADDLE_CLIENT_TOKEN`) and a **server API key** (`ITK_PADDLE_API_KEY`, needs customers and portal sessions write access).
4. **Developer tools → Notifications:** destination `https://<your-domain>/billing/paddle`, events `subscription.created`, `.updated`, `.canceled`, `.past_due`, `.paused`. Copy the **secret key** into `ITK_PADDLE_WEBHOOK_SECRET`.
5. **Checkout → Checkout settings → Default payment link / approved domains:** add your domain. Paddle must also approve the website: it checks `/pricing`, `/terms` and `/refunds` (they exist on the app's own address).
6. In `.env`: `ITK_BILLING=on`, `ITK_PADDLE_ENV=sandbox`, the values above; then `./deploy/cloudrun.sh`. It enables Firestore, creates the database, gives the service access and stores the secrets.
7. **Test in the sandbox** with card `4242 4242 4242 4242`, any future date, any CVC: connect from Claude → plan page → trial starts → saving works. Cancel from the account link → still works until the end of the period.
8. **Go live:** switch to the live Paddle account, repeat steps 2 to 4 there, set `ITK_PADDLE_ENV=production` with the live ids, redeploy.

### Promo codes (3 months free, no card)
```
gcloud auth application-default login
ITK_FIRESTORE_PROJECT=<project-id> uv run python -m intheknow.billing codes --n 5 --note "Ana, Salon Aurora"
ITK_FIRESTORE_PROJECT=<project-id> uv run python -m intheknow.billing codes --list
```
Each code looks like `ITK-7F3K-9QPA`, works once, and is entered on the plan page while connecting (or later on the account page). Only a hash is stored, so **copy the codes when they are printed**. The note is only for you.

### What owners see
| Situation | What happens |
|---|---|
| New owner connects | After Google sign-in: plan page (Yearly / Monthly, "Start 14 days free", "Have a code?"). Connecting finishes only when a plan or code is active. |
| Owner who connected before billing was on | Keeps working with the sign-in they already have, but has no plan yet: reading works, the first save shows their plan link, and reconnecting shows the plan page. Announce billing to beta users first and give them a code. |
| Free period ends, nothing active | Reading works. Saving answers "Not saved…" with their account link. Dry-run previews still work. |
| Card fails | 7 days of grace while Paddle retries, then read-only. |
| 3 free months about to end | The assistant mentions it during the last 3 days. |

**Beta users:** to keep them free, create codes for them. Anyone already connected sees the plan link the first time they try to save.

## 4. Microsoft

1. **Register the app.** In the Entra admin center, go to *App registrations → New registration*:
   - Name `In The Know`.
   - Account types: *any organizational directory and personal Microsoft accounts*.
   - Redirect URI (Web): `https://<domain>/callback/microsoft`.
2. **Add a secret.** Under *Certificates & secrets*, add a client secret. Set `ITK_MICROSOFT_CLIENT_ID` to the application ID and `ITK_MICROSOFT_CLIENT_SECRET` to the secret. Note when it expires and set a reminder to renew it.
3. **Set permissions.** Under *API permissions*, add delegated Microsoft Graph permissions: `openid`, `email`, `offline_access`, `Files.ReadWrite.AppFolder`.
4. **Branding.** Add the home page, privacy URL and logo.
5. **Publisher verification** (needs a Microsoft Partner ID). Without it, some work accounts must ask their admin.

## 5. Smoke test before telling anyone

1. **Server is up:** `https://<domain>/health` returns `{"ok":true}`. `/` and `/privacy` load.
2. **Desktop:** in Claude, go to Settings → Connectors → Add custom connector, enter `https://<domain>/mcp`, and connect with Google.
3. **Setup:** in a new chat, say "Set up In The Know". After setup, check that `In The Know/memory.itk` appears in Drive.
4. **Phone:** in the Claude app, the connector is already there. Ask "how is business?" and capture a visit by voice.
5. **Files:** say "I want to add my files", open the link on the phone, upload a CSV, and come back. Claude should see it.
6. **Microsoft and ChatGPT:** repeat steps 2–5 with Microsoft, and in ChatGPT (developer mode → connectors).
7. **Revoked access:** remove access at myaccount.google.com/permissions. The next call should ask to reconnect.
8. **Logs:** read the host's logs. You should see only `tool=… ms=… ok=… owner=<hash>` lines.

## 6. After launch

- Watch errors in the logs (`ok=no`) and Google/Microsoft API quotas.
- **Before the beta grows:**
  - run the paid LLM eval with Calendar/Gmail/Square scenarios;
  - get the privacy policy and a data processing agreement reviewed by a lawyer;
  - plan for scaling beyond one instance (§1).
