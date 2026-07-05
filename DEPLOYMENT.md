# PashxD Backend — Google Cloud Run Deployment

Migration target: **Render → Google Cloud Run + Firebase**, same MongoDB Atlas database.

The backend is a FastAPI app (async, `motor`/MongoDB Atlas, custom JWT auth, SendGrid
email). This document is the full runbook for deploying and operating it on Google Cloud.

---

## 1. Architecture decision: why MongoDB stays

The app already uses **MongoDB Atlas** (`mongodb+srv://…`). The mission brief permits
keeping the existing database ("Firestore **or** the appropriate database if already
using MongoDB"). Rewriting all CRM/blog/SEO/outreach data access to Firestore's
document model would be a large change with real regression risk and no functional
gain. **Decision: keep MongoDB Atlas.** Firebase is provisioned for **Authentication**
(and optionally Storage); Firestore is left locked down (deny-all rules).

`firebase-admin` is wired in as an **optional** dependency (`app/config/firebase.py`)
for verifying Firebase ID tokens alongside the existing JWT auth. It never blocks
startup if credentials are absent.

---

## 2. What changed vs. Render

| Area | Before (Render) | After (Cloud Run) |
| --- | --- | --- |
| Platform | `render.yaml` web service | Cloud Run (container) |
| Build | `pip install` buildpack | `Dockerfile` → Artifact Registry |
| Runtime pin | `runtime.txt` (3.11.9) | `python:3.11.9-slim` base image |
| Port | `$PORT` | `$PORT` (8080 default) — unchanged |
| Secrets | Render env vars | Secret Manager (injected as env vars) |
| Logging | plain stdout | structured JSON → Cloud Logging |
| Shutdown | default | `--timeout-graceful-shutdown 25`, lifespan closes Mongo |

Removed: `render.yaml`, `runtime.txt`.
Added: `Dockerfile`, `.dockerignore`, `.gcloudignore`, `cloudbuild.yaml`,
`deploy/setup-gcp.sh`, `deploy/create-secrets.sh`, `firebase.json`, `firebase/*`,
`app/config/logging_config.py`, `app/config/firebase.py`, `.env.example`.

---

## 3. Prerequisites

- `gcloud` CLI installed and authenticated (`gcloud auth login`). **Not yet installed
  on this machine** — install via https://cloud.google.com/sdk/docs/install or
  `brew install --cask google-cloud-sdk`.
- A **billing account** (Cloud Run/Artifact Registry/Secret Manager are billable).
  List with `gcloud billing accounts list`.
- Docker (for local image builds — optional, Cloud Build builds remotely).
- The production `.env` values (MongoDB Atlas URI, JWT secret, admin creds, SendGrid key).

---

## 4. One-command-per-step deploy

```bash
# 0. Set identifiers
export PROJECT_ID=pashxd-prod
export BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX
export REGION=europe-west1

# 1. Provision GCP: project, APIs, Artifact Registry, service accounts, IAM
./deploy/setup-gcp.sh

# 2. Load secrets into Secret Manager from your local .env
PROJECT_ID=$PROJECT_ID ./deploy/create-secrets.sh .env

# 3. Build + push + deploy via Cloud Build
gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=$REGION,_SERVICE=pashxd-backend,_REPO=pashxd

# 4. Get the URL
gcloud run services describe pashxd-backend --region=$REGION \
  --format='value(status.url)'
```

Firebase (Auth + rules), one-time:

```bash
firebase use $PROJECT_ID
firebase deploy --only firestore:rules,storage:rules
# Enable Email/Password (or chosen providers) in the Firebase console → Authentication.
```

---

## 5. Environment variables

**Secrets** (Secret Manager → injected as env vars by `--set-secrets`):

| Secret | Purpose |
| --- | --- |
| `MONGO_URL` | MongoDB Atlas connection string |
| `JWT_SECRET` | Signing key for auth tokens |
| `ADMIN_EMAIL` | Seeded admin login |
| `ADMIN_PASSWORD` | Seeded admin password |
| `SENDGRID_API_KEY` | Transactional email |
| `CORS_ORIGINS` | Extra allowed origins (comma-separated) |

**Non-secret config** (`--set-env-vars` in `cloudbuild.yaml`):

| Var | Value |
| --- | --- |
| `DB_NAME` | `pashxd` |
| `JWT_ALGORITHM` | `HS256` |
| `JWT_EXPIRE_HOURS` | `24` |
| `FRONTEND_URL` | `https://pashx.com` |
| `ADMIN_URL` | `https://admin.pashx.com` |
| `SENDGRID_FROM_EMAIL` | `info@pashx.com` |
| `SENDGRID_FROM_NAME` | `PashxD` |
| `LOG_FORMAT` | `json` |
| `GOOGLE_CLOUD_PROJECT` | `$PROJECT_ID` (auto) |
| `PORT` | `8080` (Cloud Run injects) |

See `.env.example` for the full local-dev template.

---

## 6. IAM (least privilege)

- **Runtime SA** `pashxd-backend-run@…`: only `secretmanager.secretAccessor`,
  `logging.logWriter`, `monitoring.metricWriter`.
- **Cloud Build SA** `PROJECT_NUMBER@cloudbuild…`: `run.admin`,
  `artifactregistry.writer`, `iam.serviceAccountUser` (to deploy as the runtime SA).
- The service runs `--allow-unauthenticated` because it is a public API (CORS + JWT
  guard sensitive routes). Restrict with `--no-allow-unauthenticated` + IAP if the
  API should be private.

---

## 7. Custom domain

```bash
gcloud run domain-mappings create --service=pashxd-backend \
  --domain=api.pashx.com --region=$REGION
```
Then add the returned DNS records at your registrar. Update `CORS_ORIGINS` /
`FRONTEND_URL` / `ADMIN_URL` if origins change.

---

## 8. Networking note — MongoDB Atlas allowlist

Cloud Run uses dynamic egress IPs. Either:
- Set Atlas network access to `0.0.0.0/0` (relies on SCRAM auth + TLS), **or**
- Attach a **Serverless VPC connector** + **Cloud NAT** for a static egress IP and
  allowlist that IP in Atlas (recommended for production).

---

## 9. Observability

- **Logs**: structured JSON, auto-parsed by Cloud Logging (severity + message).
  `gcloud run services logs read pashxd-backend --region=$REGION`
- **Monitoring**: Cloud Run exports request count, latency, instance count, memory
  by default. Add uptime check against `/health` and alerting in Cloud Monitoring.
- **Health check**: `GET /health` → `{"status":"healthy"}`.

---

## 10. Deployment checklist

- [ ] `gcloud` installed + authenticated
- [ ] Billing account linked to project
- [ ] `./deploy/setup-gcp.sh` run (APIs, registry, SAs, IAM)
- [ ] Secrets loaded (`./deploy/create-secrets.sh`)
- [ ] MongoDB Atlas network access allows Cloud Run egress
- [ ] `gcloud builds submit` succeeds
- [ ] Service URL returns 200 on `/health`
- [ ] Login works (`POST /api/auth/login`)
- [ ] Firebase rules deployed; Auth providers enabled (if using Firebase Auth)
- [ ] Custom domain mapped + DNS + CORS updated
- [ ] Frontend/admin `VITE_API_URL` (or equivalent) pointed at the new URL
- [ ] Old Render service scaled down / deleted after cutover

---

## 11. Rollback plan

Cloud Run keeps every revision.

```bash
# List revisions
gcloud run revisions list --service=pashxd-backend --region=$REGION

# Roll 100% traffic back to a known-good revision
gcloud run services update-traffic pashxd-backend --region=$REGION \
  --to-revisions=pashxd-backend-00001-abc=100
```

Full fallback: the Render service can be re-enabled from `render.yaml` in git history
(`git show <sha>:backend/render.yaml`) until the new deployment is confirmed stable.
DNS cutover is the last step, so reverting the domain record restores Render instantly.

---

## 12. Known limitations

- **Not yet deployed to a live GCP project** — this repo contains the full,
  verified deployment tooling, but running `setup-gcp.sh` provisions billable,
  partly-irreversible resources and requires `gcloud` + a billing account. That step
  needs a human with billing authority.
- **Firestore is unused** (deny-all rules). Data stays in MongoDB Atlas by design.
- **Firebase Auth is optional/additive** — existing JWT auth is unchanged; Firebase
  ID-token verification is available via `app/config/firebase.py` but not yet wired
  into any route.
- Single uvicorn worker per instance; Cloud Run scales horizontally by instance count.

---

## 13. Local verification performed

The image was built and run locally against a MongoDB container. **9/9 integration
checks passed**: `/health`, `/api/`, `/docs`, `POST /api/status` (DB write),
`POST /api/demo-requests` (DB write + CRM auto-conversion), 403 on unauthenticated
admin route, `POST /api/auth/login` (JWT issued), and 200 on the admin route with the
token. Structured JSON logging and graceful SIGTERM shutdown (clean Mongo close) were
both confirmed.
