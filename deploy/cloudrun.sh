#!/usr/bin/env bash
# Deploy the In The Know hosted connector to Google Cloud Run (EU, exactly one instance).
# Safe to re-run: it updates the service and only touches secrets that changed.
#
#   gcloud auth login && gcloud config set project <project-id>
#   ./deploy/cloudrun.sh
#
# Reads ITK_GOOGLE_CLIENT_ID / ITK_GOOGLE_CLIENT_SECRET (and optional ITK_GOOGLE_API_KEY, ITK_GOOGLE_APP_ID,
# ITK_OPERATOR, ITK_CONTACT) from .env. The production seal key is generated once in Secret Manager and never
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

ENV="ITK_BASE_URL=$URL@ITK_GOOGLE_CLIENT_ID=$ITK_GOOGLE_CLIENT_ID@ITK_HOSTING=Google Cloud Run in $REGION (EU)@ITK_LOG_LEVEL=INFO"
for v in ITK_GOOGLE_API_KEY ITK_GOOGLE_APP_ID ITK_OPERATOR ITK_CONTACT ITK_POLICY_DATE; do
  [ -n "${!v:-}" ] && ENV="$ENV@$v=${!v}"
done

echo "3/4 Building and deploying (a few minutes)…"
gcloud run deploy "$SERVICE" --project "$PROJECT" --region "$REGION" --source . --quiet \
  --allow-unauthenticated --min-instances 1 --max-instances 1 --cpu 1 --memory 1Gi \
  --concurrency 80 --timeout 3600 \
  --set-env-vars "^@^$ENV" \
  --set-secrets "ITK_SEAL_KEYS=itk-seal-keys:latest,ITK_GOOGLE_CLIENT_SECRET=itk-google-client-secret:latest"

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
