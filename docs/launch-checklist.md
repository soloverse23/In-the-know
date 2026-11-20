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
| `PORT` | set by host | Default 8080 in the image |

**Secrets** (seal keys, client secrets) go into the host's secret store, never into the repo or the image.

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
