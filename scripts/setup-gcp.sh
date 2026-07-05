#!/usr/bin/env bash
# ── One-time Google Cloud + Firebase provisioning for PashxD ─────────
# Idempotent: safe to re-run. Requires: gcloud CLI authenticated as a
# project owner/editor, and (optionally) firebase CLI for rules deploy.
#
# Usage:
#   PROJECT_ID=pashxd-prod REGION=europe-west1 ./scripts/setup-gcp.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID, e.g. PROJECT_ID=pashxd-prod}"
REGION="${REGION:-europe-west1}"
REPO_NAME="${REPO_NAME:-pashxd}"
SERVICE_NAME="${SERVICE_NAME:-pashxd-api}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-pashxd-api-runtime}"
DEPLOY_SA_NAME="${DEPLOY_SA_NAME:-pashxd-deployer}"

RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
DEPLOY_SA="${DEPLOY_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "── Project ──────────────────────────────────────────────"
if ! gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  echo "Creating project $PROJECT_ID (link a billing account afterwards!)"
  gcloud projects create "$PROJECT_ID"
fi
gcloud config set project "$PROJECT_ID"

if [ -n "${BILLING_ACCOUNT_ID:-}" ]; then
  gcloud billing projects link "$PROJECT_ID" --billing-account "$BILLING_ACCOUNT_ID"
fi

echo "── APIs ─────────────────────────────────────────────────"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  logging.googleapis.com \
  monitoring.googleapis.com \
  iam.googleapis.com \
  cloudresourcemanager.googleapis.com \
  firebase.googleapis.com \
  firestore.googleapis.com \
  identitytoolkit.googleapis.com \
  firebasestorage.googleapis.com

echo "── Artifact Registry ────────────────────────────────────"
if ! gcloud artifacts repositories describe "$REPO_NAME" --location="$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPO_NAME" \
    --repository-format=docker \
    --location="$REGION" \
    --description="PashxD container images"
fi

echo "── Service accounts (least privilege) ───────────────────"
# Runtime SA: what the Cloud Run service runs as. It gets ONLY secret
# access (per-secret, below) + Firebase/Firestore data access.
if ! gcloud iam service-accounts describe "$RUNTIME_SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
    --display-name="PashxD API Cloud Run runtime"
fi
# Firestore + Firebase Auth admin (custom claims) + Storage objects.
for role in roles/datastore.user roles/firebaseauth.admin; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${RUNTIME_SA}" --role="$role" \
    --condition=None --quiet >/dev/null
done

# Deploy SA: used by CI (Cloud Build / GitHub Actions) to build & deploy.
if ! gcloud iam service-accounts describe "$DEPLOY_SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$DEPLOY_SA_NAME" \
    --display-name="PashxD CI deployer"
fi
for role in roles/run.developer roles/artifactregistry.writer roles/cloudbuild.builds.editor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOY_SA}" --role="$role" \
    --condition=None --quiet >/dev/null
done
# Deployer must be able to act as the runtime SA when deploying.
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:${DEPLOY_SA}" \
  --role="roles/iam.serviceAccountUser" --quiet >/dev/null

echo "── Secret Manager ───────────────────────────────────────"
# Creates empty secrets on first run; add values with:
#   printf 'VALUE' | gcloud secrets versions add NAME --data-file=-
for secret in pashxd-mongo-url pashxd-jwt-secret pashxd-sendgrid-api-key pashxd-admin-email pashxd-admin-password; do
  if ! gcloud secrets describe "$secret" >/dev/null 2>&1; then
    gcloud secrets create "$secret" --replication-policy="automatic"
    echo "  created $secret (no value yet — add one!)"
  fi
  # Runtime SA may access ONLY these specific secrets (not project-wide).
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
done

echo "── Firestore (native mode) ──────────────────────────────"
if ! gcloud firestore databases describe --database='(default)' >/dev/null 2>&1; then
  gcloud firestore databases create --location="$REGION" --type=firestore-native
fi

echo "── Firebase ─────────────────────────────────────────────"
cat <<EOF
Firebase steps (once, via firebase CLI or console):
  firebase projects:addfirebase ${PROJECT_ID}     # attach Firebase to the GCP project
  firebase deploy --only firestore:rules,firestore:indexes,storage --project ${PROJECT_ID}
  # Enable Email/Password sign-in: Firebase console → Authentication → Sign-in method
EOF

echo
echo "✅ GCP provisioning complete for ${PROJECT_ID}."
echo "   Next: add secret values, then run scripts/deploy.sh"
