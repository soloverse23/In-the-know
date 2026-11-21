#!/usr/bin/env bash
# Deploy the In The Know hosted connector to Google Cloud Run (EU, exactly one instance).
# Safe to re-run: it updates the service and only touches secrets that changed.
#
#   gcloud auth login && gcloud config set project <project-id>
#   ./deploy/cloudrun.sh
#
# Reads ITK_GOOGLE_CLIENT_ID / ITK_GOOGLE_CLIENT_SECRET (and optional ITK_GOOGLE_API_KEY, ITK_GOOGLE_APP_ID,
# ITK_OPERATOR, ITK_CONTACT) from .env.
#
# Billing is off unless .env has ITK_BILLING=on. Then it also needs (see docs/launch-checklist.md, "Billing"):
#   ITK_PADDLE_ENV=sandbox|production   ITK_PADDLE_CLIENT_TOKEN   ITK_PADDLE_API_KEY   ITK_PADDLE_WEBHOOK_SECRET
#   ITK_PADDLE_PRICE_MONTHLY  ITK_PADDLE_PRICE_YEARLY  [ITK_PADDLE_PRICE_MONTHLY_NOTRIAL  ITK_PADDLE_PRICE_YEARLY_NOTRIAL] The production seal key is generated once in Secret Manager and never
# printed; it is NOT the one in .env.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-europe-west1}"
SERVICE="${SERVICE:-intheknow}"
[ -n "$PROJECT" ] || { echo "No Google Cloud project selected. Run: gcloud config set project <project-id>"; exit 1; }
[ -f .env ] || { echo ".env not found in $(pwd)"; exit 1; }
set -a; . ./.env; set +a
: "${ITK_GOOGLE_CLIENT_ID:?ITK_GOOGLE_CLIENT_ID is missing in .env}"
: "${ITK_GOOGLE_CLIENT_SECRET:?ITK_GOOGLE_CLIENT_SECRET is missing in .env}"

BILLING_ON=""
case "${ITK_BILLING:-}" in on|1|true|yes) BILLING_ON=1 ;; esac
if [ -n "$BILLING_ON" ]; then
  for v in ITK_PADDLE_CLIENT_TOKEN ITK_PADDLE_API_KEY ITK_PADDLE_WEBHOOK_SECRET ITK_PADDLE_PRICE_MONTHLY ITK_PADDLE_PRICE_YEARLY; do
    [ -n "${!v:-}" ] || { echo "ITK_BILLING=on but $v is missing in .env"; exit 1; }
  done
fi

NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
URL="https://${SERVICE}-${NUMBER}.${REGION}.run.app"
RUNTIME_SA="${NUMBER}-compute@developer.gserviceaccount.com"
echo "Project $PROJECT ($NUMBER) · region $REGION · $URL"

BILLING="$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null || true)"
if [ "$BILLING" = "False" ]; then
  echo "Billing is not enabled for $PROJECT. Link a billing account in the Google Cloud console (Billing), then re-run."; exit 1
fi

echo "1/4 Enabling services…"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com --project "$PROJECT" --quiet
if [ -n "$BILLING_ON" ]; then
  gcloud services enable firestore.googleapis.com --project "$PROJECT" --quiet
  # the billing record (plan status, used codes): one small EU database, no business data
  if ! gcloud firestore databases describe --project "$PROJECT" >/dev/null 2>&1; then
    gcloud firestore databases create --project "$PROJECT" --location="$REGION" --type=firestore-native --quiet >/dev/null
    echo "   created the Firestore database in $REGION"
  fi
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$RUNTIME_SA" \
    --role roles/datastore.user --condition=None --quiet >/dev/null
fi

echo "2/4 Secrets…"
ensure_secret() {   # name value create_only
  if ! gcloud secrets describe "$1" --project "$PROJECT" >/dev/null 2>&1; then
    printf %s "$2" | gcloud secrets create "$1" --project "$PROJECT" --replication-policy=user-managed \
      --locations="$REGION" --data-file=- --quiet >/dev/null
    echo "   created $1"
  elif [ "${3:-}" != "create_only" ] && [ "$(gcloud secrets versions access latest --secret "$1" --project "$PROJECT")" != "$2" ]; then
    printf %s "$2" | gcloud secrets versions add "$1" --project "$PROJECT" --data-file=- --quiet >/dev/null
    echo "   updated $1"
  fi
  gcloud secrets add-iam-policy-binding "$1" --project "$PROJECT" --member "serviceAccount:$RUNTIME_SA" \
    --role roles/secretmanager.secretAccessor --quiet >/dev/null
}
SEAL="k1:$(python3 -c 'import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode())')"
ensure_secret itk-seal-keys "$SEAL" create_only        # never replaced: replacing it would sign every owner out
ensure_secret itk-google-client-secret "$ITK_GOOGLE_CLIENT_SECRET"
SECRETS="ITK_SEAL_KEYS=itk-seal-keys:latest,ITK_GOOGLE_CLIENT_SECRET=itk-google-client-secret:latest"
if [ -n "$BILLING_ON" ]; then
  ensure_secret itk-paddle-api-key "$ITK_PADDLE_API_KEY"
  ensure_secret itk-paddle-webhook-secret "$ITK_PADDLE_WEBHOOK_SECRET"
  SECRETS="$SECRETS,ITK_PADDLE_API_KEY=itk-paddle-api-key:latest,ITK_PADDLE_WEBHOOK_SECRET=itk-paddle-webhook-secret:latest"
fi

ENV="ITK_BASE_URL=$URL@ITK_GOOGLE_CLIENT_ID=$ITK_GOOGLE_CLIENT_ID@ITK_HOSTING=Google Cloud Run in $REGION (EU)@ITK_LOG_LEVEL=INFO"
for v in ITK_GOOGLE_API_KEY ITK_GOOGLE_APP_ID ITK_OPERATOR ITK_CONTACT ITK_POLICY_DATE; do
  [ -n "${!v:-}" ] && ENV="$ENV@$v=${!v}"
done
if [ -n "$BILLING_ON" ]; then
  ENV="$ENV@ITK_BILLING=on@ITK_PADDLE_ENV=${ITK_PADDLE_ENV:-sandbox}@ITK_PADDLE_CLIENT_TOKEN=$ITK_PADDLE_CLIENT_TOKEN"
  ENV="$ENV@ITK_PADDLE_PRICE_MONTHLY=$ITK_PADDLE_PRICE_MONTHLY@ITK_PADDLE_PRICE_YEARLY=$ITK_PADDLE_PRICE_YEARLY"
  for v in ITK_PADDLE_PRICE_MONTHLY_NOTRIAL ITK_PADDLE_PRICE_YEARLY_NOTRIAL; do
    [ -n "${!v:-}" ] && ENV="$ENV@$v=${!v}"
  done
fi

echo "3/4 Building and deploying (a few minutes)…"
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --source . --quiet \
  --allow-unauthenticated --min-instances 1 --max-instances 1 --cpu 1 --memory 1Gi \
  --concurrency 80 --timeout 3600 \
  --set-env-vars "^@^$ENV" \
  --set-secrets "$SECRETS"

echo "4/4 Checking…"
for i in $(seq 1 20); do curl -sf "$URL/health" >/dev/null && break; sleep 3; done
curl -sf "$URL/health" >/dev/null || { echo "The service did not answer at $URL/health. See: gcloud run services logs read $SERVICE --region $REGION"; exit 1; }

cat <<EOF

Done. In The Know is running at $URL

Next:
  1. Google Auth Platform → Clients → your web client → Authorized redirect URIs → add:
       $URL/callback/google
  2. In Claude: Settings → Connectors → Add custom connector → $URL/mcp
EOF
if [ -n "$BILLING_ON" ]; then
  cat <<EOF
  3. Paddle → Developer tools → Notifications → add a destination:
       $URL/billing/paddle
     events: subscription.created, subscription.updated, subscription.canceled, subscription.past_due, subscription.paused
  4. Make promo codes:  uv run python -m intheknow.billing codes --n 5 --note "who they are for"
     (needs: gcloud auth application-default login, and ITK_FIRESTORE_PROJECT=$PROJECT)
EOF
fi
