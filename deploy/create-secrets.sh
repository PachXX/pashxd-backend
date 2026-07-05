#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Load PashxD secrets into Google Secret Manager.
#
# Reads values from a local .env file (NOT committed) and creates/updates one
# Secret Manager secret per sensitive key. Non-secret config (DB_NAME, URLs,
# LOG_FORMAT, etc.) is passed as plain env vars at deploy time — see cloudbuild.yaml.
#
# Usage:
#   export PROJECT_ID=pashxd-prod
#   ./deploy/create-secrets.sh path/to/.env
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
ENV_FILE="${1:-.env}"
[[ -f "$ENV_FILE" ]] || { echo "env file not found: $ENV_FILE" >&2; exit 1; }

gcloud config set project "$PROJECT_ID" >/dev/null

# Only these keys are treated as secrets. Everything else stays a plain env var.
SECRET_KEYS=(MONGO_URL JWT_SECRET ADMIN_EMAIL ADMIN_PASSWORD SENDGRID_API_KEY CORS_ORIGINS)

upsert_secret() {
  local name="$1" value="$2"
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
    echo "  ↺ updated $name"
  else
    printf '%s' "$value" | gcloud secrets create "$name" --replication-policy=automatic --data-file=- >/dev/null
    echo "  ＋ created $name"
  fi
}

echo "▶ Loading secrets from $ENV_FILE"
for key in "${SECRET_KEYS[@]}"; do
  # Extract KEY=value (strip surrounding quotes, ignore comments).
  line=$(grep -E "^${key}=" "$ENV_FILE" | head -1 || true)
  [[ -z "$line" ]] && { echo "  ⚠ $key not in $ENV_FILE — skipping"; continue; }
  value="${line#*=}"
  value="${value%\"}"; value="${value#\"}"
  upsert_secret "$key" "$value"
done

echo "✅ Secrets loaded. Grant access to the runtime SA is handled by setup-gcp.sh"
