#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# One-time Google Cloud provisioning for the PashxD backend.
#
# This script is IDEMPOTENT-ish but performs BILLABLE, mostly-irreversible actions
# (project creation, API enablement, service accounts, secrets). Review before
# running. It requires the `gcloud` CLI, authenticated as an account with
# Owner/Editor on the org or billing account.
#
# Usage:
#   export PROJECT_ID=pashxd-prod
#   export BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX   # from: gcloud billing accounts list
#   export REGION=europe-west1
#   ./deploy/setup-gcp.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${REGION:=europe-west1}"
REPO="${REPO:-pashxd}"
SERVICE="${SERVICE:-pashxd-backend}"
RUNTIME_SA="${RUNTIME_SA:-pashxd-backend-run}"

echo "▶ Project: $PROJECT_ID | Region: $REGION"

# 1. Create the project if it doesn't exist, and link billing.
if ! gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  echo "▶ Creating project $PROJECT_ID"
  gcloud projects create "$PROJECT_ID"
fi
gcloud config set project "$PROJECT_ID"

if [[ -n "${BILLING_ACCOUNT:-}" ]]; then
  echo "▶ Linking billing account"
  gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT"
fi

# 2. Enable required APIs.
echo "▶ Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  iam.googleapis.com \
  firestore.googleapis.com \
  firebase.googleapis.com \
  identitytoolkit.googleapis.com

# 3. Artifact Registry repository (Docker).
if ! gcloud artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1; then
  echo "▶ Creating Artifact Registry repo $REPO"
  gcloud artifacts repositories create "$REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="PashxD backend images"
fi

# 4. Runtime service account (least privilege — no broad roles by default).
SA_EMAIL="${RUNTIME_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  echo "▶ Creating runtime service account $RUNTIME_SA"
  gcloud iam service-accounts create "$RUNTIME_SA" \
    --display-name="PashxD backend Cloud Run runtime"
fi

# Runtime SA needs to read secrets and write logs/metrics. Nothing more.
for ROLE in roles/secretmanager.secretAccessor roles/logging.logWriter roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" --role="$ROLE" --condition=None >/dev/null
done

# 5. Let the Cloud Build SA deploy to Cloud Run as the runtime SA.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
CB_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"
for ROLE in roles/run.admin roles/artifactregistry.writer roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${CB_SA}" --role="$ROLE" --condition=None >/dev/null
done

echo "✅ GCP provisioning complete."
echo "   Next: ./deploy/create-secrets.sh   then   gcloud builds submit --config cloudbuild.yaml \\"
echo "         --substitutions=_REGION=${REGION},_SERVICE=${SERVICE},_REPO=${REPO}"
