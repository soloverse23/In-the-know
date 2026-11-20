#!/usr/bin/env bash
# In The Know for yourself, now: runs on this Mac and is reachable through your ngrok domain.
# No Google/Microsoft sign-in and no cloud account. One memory (default ~/InTheKnow/business.db, the same file the
# Claude Desktop extension uses). The connector address contains a secret key: keep it private.
#
#   ./deploy/personal.sh            start (Ctrl+C stops everything)
#
# .env needs ITK_NGROK_DOMAIN=<your-domain>.ngrok-free.dev; ITK_PERSONAL_KEY is created on the first run.
set -euo pipefail
cd "$(dirname "$0")/.."

touch .env && chmod 600 .env
set -a; . ./.env; set +a
: "${ITK_NGROK_DOMAIN:?Add ITK_NGROK_DOMAIN=<your-domain>.ngrok-free.dev to .env}"
if [ -z "${ITK_PERSONAL_KEY:-}" ]; then
  ITK_PERSONAL_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
  printf '\nITK_PERSONAL_KEY=%s\n' "$ITK_PERSONAL_KEY" >> .env
  export ITK_PERSONAL_KEY
fi
PORT="${PORT:-8000}"
URL="https://$ITK_NGROK_DOMAIN"

if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "Something is already running on port $PORT. Stop it or run with PORT=8010 ./deploy/personal.sh"; exit 1
fi

uv run python -m intheknow.hosted --personal --base-url "$URL" --port "$PORT" &
SERVER=$!
NGROK=""
cleanup() { [ -n "$NGROK" ] && kill "$NGROK" 2>/dev/null; kill "$SERVER" 2>/dev/null; wait 2>/dev/null; echo "Stopped."; }
trap cleanup EXIT INT TERM

for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 1; done
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null || { echo "The server did not start (see the lines above)."; exit 1; }

# --inspect=false: ngrok keeps no copy of requests on this Mac
ngrok http --url="$URL" "$PORT" --inspect=false --log=false >/dev/null &
NGROK=$!
for _ in $(seq 1 30); do curl -sf "$URL/health" >/dev/null 2>&1 && break; sleep 1; done
curl -sf "$URL/health" >/dev/null || { echo "ngrok did not connect $URL (is the domain right, and ngrok not running elsewhere?)."; exit 1; }

command -v caffeinate >/dev/null && caffeinate -i -w "$SERVER" &   # keep the Mac awake while the server runs

cat <<EOF

In The Know is running.

  Connector address (private, do not share):
  $URL/$ITK_PERSONAL_KEY/mcp

In Claude: Settings → Connectors → Add custom connector → paste the address → Add.
It then works on your phone too, as long as this Mac is on and this window stays open. Ctrl+C stops it.
EOF
wait "$SERVER"
