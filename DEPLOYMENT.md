# PashxD Backend — Google Cloud Run + Firebase Deployment

The backend has been migrated **from Render to Google Cloud Run**, with
Firebase attached to the same Google Cloud project. MongoDB (Atlas)
remains the system of record; Firestore rules are deployed locked-down
for future client-side use.

## Architecture

```
Vercel (pashx.com / admin.pashx.com)
        │  HTTPS + CORS
        ▼
Cloud Run: pashxd-api  ── runs as pashxd-api-runtime SA
        │  ├── Secret Manager  (MONGO_URL, JWT_SECRET, SENDGRID_API_KEY,
        │  │                    ADMIN_EMAIL, ADMIN_PASSWORD)
        │  ├── Cloud Logging   (structured JSON logs, automatic)
        │  ├── Cloud Monitoring (built-in Cloud Run metrics)
        │  └── Firebase Admin SDK (Auth token verification, ADC)
        ▼
MongoDB Atlas (unchanged)      SendGrid (unchanged)
```

## One-time provisioning

```bash
# 0. The Firebase project already exists: pashxd-e56c5 (created via the
#    Firebase console, web app registered). Its backing GCP project ID
#    is the same string — use that as PROJECT_ID below.

# 1. Provision GCP: APIs, Artifact Registry, service accounts,
#    IAM (least privilege), Secret Manager, Firestore.
PROJECT_ID=pashxd-e56c5 REGION=europe-west1 ./scripts/setup-gcp.sh
# (add BILLING_ACCOUNT_ID=XXXXXX-XXXXXX-XXXXXX to link billing, if not
#  already linked from the Firebase console)

# 2. Add secret values
printf '%s' 'mongodb+srv://...'        | gcloud secrets versions add pashxd-mongo-url --data-file=-
openssl rand -base64 48 | tr -d '\n'   | gcloud secrets versions add pashxd-jwt-secret --data-file=-
printf '%s' 'SG.xxxxx'                 | gcloud secrets versions add pashxd-sendgrid-api-key --data-file=-
printf '%s' 'admin@pashx.com'          | gcloud secrets versions add pashxd-admin-email --data-file=-
printf '%s' 'a-strong-admin-password'  | gcloud secrets versions add pashxd-admin-password --data-file=-

# 3. Deploy Firestore/Storage rules (Firebase is already attached to
#    this project — no need to run `firebase projects:addfirebase`)
firebase deploy --only firestore:rules,firestore:indexes,storage --project pashxd-e56c5
# Console: Authentication → Sign-in method → enable Email/Password

# 4. Allow Cloud Run egress in MongoDB Atlas
#    Atlas → Network Access → allow 0.0.0.0/0 (or set up a static egress
#    IP via Serverless VPC Access + Cloud NAT for a pinned allowlist).
```

## Deploy

```bash
PROJECT_ID=pashxd-e56c5 REGION=europe-west1 ./scripts/deploy.sh
```

Or set up continuous deployment from GitHub:

```bash
gcloud builds triggers create github \
  --repo-owner=PachXX --repo-name=pashxd-backend \
  --branch-pattern='^main$' --build-config=cloudbuild.yaml
```

After the first deploy, point the frontend at the service:
* Vercel → pashxd-frontend → Environment Variables →
  `VITE_API_URL=https://<cloud-run-service-url>` and redeploy.
* The frontend's `VITE_FIREBASE_*` variables (see `pashxd-frontend/.env.example`)
  point at the same `pashxd-e56c5` Firebase project and don't need to
  change per-environment — they're the public web SDK config, not secrets.

### Custom domain (optional)

```bash
gcloud beta run domain-mappings create --service pashxd-api \
  --domain api.pashx.com --region europe-west1
# then add the shown DNS records, and set VITE_API_URL=https://api.pashx.com
```

## Environment variables

| Variable | Source | Required | Notes |
|---|---|---|---|
| `MONGO_URL` | Secret Manager `pashxd-mongo-url` | ✅ prod | App refuses to start on Cloud Run without it |
| `JWT_SECRET` | Secret Manager `pashxd-jwt-secret` | ✅ prod | App refuses to start on Cloud Run without it |
| `SENDGRID_API_KEY` | Secret Manager `pashxd-sendgrid-api-key` | for email | |
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | Secret Manager | optional | Seeds the first admin; skipped when unset (no default creds in prod) |
| `DB_NAME` | env var | no (default `pashxd`) | |
| `FRONTEND_URL` / `ADMIN_URL` / `CORS_ORIGINS` | env var | no | CORS allowlist |
| `BACKEND_URL` | env var (set by deploy.sh) | for email tracking links | |
| `JWT_EXPIRE_HOURS` / `JWT_ALGORITHM` | env var | no | defaults 24 / HS256 |
| `FIREBASE_PROJECT_ID` | env var (deploy.sh sets it) | enables Firebase | Admin SDK uses ADC — no key file |
| `FIREBASE_STORAGE_BUCKET` | env var | optional | only if Storage is used |
| `SENDGRID_FROM_EMAIL` / `SENDGRID_FROM_NAME` / `OUTREACH_CC` | env var | no | |
| `LOG_LEVEL` / `LOG_FORMAT` | env var | no | JSON logging is automatic on Cloud Run |

## Firebase configuration

* **Admin SDK**: initialized at startup via Application Default
  Credentials (`app/config/firebase.py`). No service-account key files —
  the Cloud Run runtime SA is the identity.
* **Authentication**: the API accepts Firebase ID tokens as bearer
  tokens in addition to legacy JWTs (`app/middleware/auth.py`). Admin
  access requires the custom claim `role=admin`:
  ```python
  from firebase_admin import auth
  auth.set_custom_user_claims(uid, {"role": "admin"})
  ```
* **Firestore**: provisioned (native mode); `firestore.rules` denies all
  client writes, allows reads only to admins. MongoDB stays primary.
* **Storage**: `storage.rules` denies client writes; backend uses the
  Admin SDK if/when uploads are added.
* **Indexes**: `firestore.indexes.json` (none needed yet).

## IAM (least privilege)

| Principal | Roles |
|---|---|
| `pashxd-api-runtime@…` (Cloud Run) | `secretmanager.secretAccessor` on the 5 pashxd-* secrets only; `datastore.user`; `firebaseauth.admin` |
| `pashxd-deployer@…` (CI) | `run.developer`, `artifactregistry.writer`, `cloudbuild.builds.editor`, `iam.serviceAccountUser` on the runtime SA |

## Operations

* **Logs**: `gcloud run services logs read pashxd-api --region europe-west1`
  — structured JSON (severity, message, source location).
* **Health**: `GET /health` (liveness, used by the startup probe) and
  `GET /health/ready` (verifies MongoDB connectivity, returns 503 when down).
* **Monitoring**: Cloud Run built-in dashboards (requests, latency, 5xx,
  instance count). Recommended alert: 5xx ratio > 1% for 5 min.
* **Scaling**: 0–10 instances, 80 concurrent requests, 512 Mi / 1 CPU.
* **Graceful shutdown**: SIGTERM → uvicorn drains → lifespan closes the
  Mongo client (verified: ~1 s clean shutdown).

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/test_unit.py -v                     # no server needed

# End-to-end against any live deployment:
docker build --network host -t pashxd-api:test .
docker network create pashxd-test
docker run -d --name pashxd-mongo --network pashxd-test mongo:7
docker run -d --name pashxd-api --network pashxd-test -p 8080:8080 \
  -e K_SERVICE=pashxd-api -e MONGO_URL=mongodb://pashxd-mongo:27017 \
  -e JWT_SECRET=dev -e ADMIN_EMAIL=admin@pashx.com -e ADMIN_PASSWORD=devpass \
  pashxd-api:test
BASE_URL=http://localhost:8080 TEST_ADMIN_EMAIL=admin@pashx.com \
  TEST_ADMIN_PASSWORD=devpass pytest tests/ -v
```

## Local development (Firebase Emulator Suite)

The full stack — API, MongoDB, and Firebase Auth/Firestore emulators —
runs locally with one command:

```bash
docker compose up --build
```

Or run the emulators directly (`npm i -g firebase-tools`):

```bash
firebase emulators:start --only auth,firestore --project demo-pashxd
```

Point the API at them with `FIREBASE_PROJECT_ID=demo-pashxd` and
`FIREBASE_AUTH_EMULATOR_HOST=localhost:9099`. The Firebase auth tests
(`tests/test_firebase_auth.py`) create emulator users (with and without
the `role=admin` claim) and assert the API's token verification and
role enforcement:

```bash
BASE_URL=http://localhost:8080 FIREBASE_AUTH_EMULATOR_URL=http://localhost:9099 \
  pytest tests/test_firebase_auth.py -v
```

## Deployment checklist

- [ ] `setup-gcp.sh` run; billing linked; all APIs enabled
- [ ] All 5 secrets have values in Secret Manager
- [ ] Firebase attached; Email/Password sign-in enabled; rules deployed
- [ ] MongoDB Atlas network access allows Cloud Run egress
- [ ] `deploy.sh` completed; `/health` and `/health/ready` return 200
- [ ] `POST /api/auth/login` works with the seeded admin
- [ ] Vercel `VITE_API_URL` updated to the Cloud Run URL; frontend redeployed
- [ ] Admin dashboard login + CRM verified in the browser
- [ ] (optional) `api.pashx.com` domain mapping + DNS
- [ ] Cloud Build trigger created for CI/CD
- [ ] Render service **suspended** (not deleted) until soak period ends

## Rollback plan

1. **Fast rollback (bad revision)** — Cloud Run keeps every revision:
   ```bash
   gcloud run services update-traffic pashxd-api \
     --region europe-west1 --to-revisions <previous-revision>=100
   ```
2. **Full rollback to Render** — the Render service still exists until
   you delete it: resume it, then set Vercel `VITE_API_URL` back to
   `https://pashxd-backend.onrender.com`. No data migration is needed in
   either direction because MongoDB Atlas is shared by both platforms.
3. Keep the Render service suspended (free) for at least one soak week
   before deleting it.

## Known limitations

* `gcloud`/`firebase` steps above require an authenticated operator; the
  scripts are idempotent and safe to re-run.
* Firebase Auth is wired in as an *additional* token type; the admin UI
  still uses the legacy `/api/auth/login` JWT flow. Migrating the UI to
  Firebase Auth clients is a separate follow-up.
* Cold starts: with `min-instances 0` the first request after idle takes
  a few seconds (uvicorn boot + Mongo connect). Set `--min-instances 1`
  (~free tier exceeded) if this matters.
* Email tracking links use `BACKEND_URL`; if you map `api.pashx.com`,
  update that env var so links don't point at the `run.app` URL.
