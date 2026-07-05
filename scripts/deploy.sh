#!/usr/bin/env bash
# ── Build & deploy PashxD API to Cloud Run ───────────────────────────
# Usage:
#   PROJECT_ID=pashxd-prod REGION=europe-west1 ./scripts/deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-europe-west1}"
REPO_NAME="${REPO_NAME:-pashxd}"
SERVICE_NAME="${SERVICE_NAME:-pashxd-api}"
RUNTIME_SA="${RUNTIME_SA:-pashxd-api-runtime@${PROJECT_ID}.iam.gserviceaccount.com}"
FRONTEND_URL="${FRONTEND_URL:-https://pashx.com}"
ADMIN_URL="${ADMIN_URL:-https://admin.pashx.com}"

IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${SERVICE_NAME}:$(git rev-parse --short HEAD)"

echo "── Building image with Cloud Build: ${IMAGE}"
gcloud builds submit --project "$PROJECT_ID" --tag "$IMAGE" .

echo "── Deploying to Cloud Run: ${SERVICE_NAME} (${REGION})"
gcloud run deploy "$SERVICE_NAME" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --image "$IMAGE" \
  --service-account "$RUNTIME_SA" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 10 \
  --concurrency 80 \
  --timeout 300 \
  --set-env-vars "DB_NAME=pashxd,FRONTEND_URL=${FRONTEND_URL},ADMIN_URL=${ADMIN_URL},JWT_EXPIRE_HOURS=24,FIREBASE_PROJECT_ID=${PROJECT_ID}" \
  --set-secrets "MONGO_URL=pashxd-mongo-url:latest,JWT_SECRET=pashxd-jwt-secret:latest,SENDGRID_API_KEY=pashxd-sendgrid-api-key:latest,ADMIN_EMAIL=pashxd-admin-email:latest,ADMIN_PASSWORD=pashxd-admin-password:latest" \
  --startup-probe "httpGet.path=/health,initialDelaySeconds=5,periodSeconds=5,failureThreshold=6"

SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --project "$PROJECT_ID" --region "$REGION" --format 'value(status.url)')

# The service needs its own public URL for email tracking links.
gcloud run services update "$SERVICE_NAME" \
  --project "$PROJECT_ID" --region "$REGION" \
  --update-env-vars "BACKEND_URL=${SERVICE_URL}" --quiet

echo
echo "✅ Deployed: ${SERVICE_URL}"
echo "   Smoke test: curl ${SERVICE_URL}/health && curl ${SERVICE_URL}/health/ready"
