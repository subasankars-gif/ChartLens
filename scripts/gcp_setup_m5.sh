#!/usr/bin/env bash
# Milestone 5 one-time setup (ADR-0016). Run in Cloud Shell as the project owner.
# Idempotent where gcloud allows: re-running prints "already exists" errors and carries on.
#
# Before running: add Firebase to the project and enable Google sign-in (console steps
# in the README, "API deployment"). This script does the rest.
set -u
PROJECT_ID=chartlens-lake-13934
PROJECT_NUMBER=1082278531047
REGION=${REGION:-us-central1}   # the bucket's region (ADR-0016)
BUCKET=chartlens-lake-13934-data
REPO=subasankars-gif/ChartLens
POOL_PRINCIPAL="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}"

gcloud config set project "$PROJECT_ID"

# Guard (M8 completion gate, A6 / readiness R12). This script binds the deployer to the
# whole repository (step "Deployer identity" below). After A6, the deployer and the
# pipeline accept only the `production` environment's OIDC subject
# (scripts/gcp_setup_m8.sh); re-running this script would silently restore access for
# any branch or old workflow. So it refuses to run, before changing anything, as soon as
# either identity carries an environment-subject binding. On a project that predates A6
# (no such binding, or the identities do not exist yet) it behaves exactly as before.
for sa in "chartlens-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
  "chartlens-pipeline@${PROJECT_ID}.iam.gserviceaccount.com"; do
  # Fail closed: "the identity does not exist" is only an empty answer from a successful
  # listing; any error (permissions, network) stops the script.
  if ! found=$(gcloud iam service-accounts list --filter="email=$sa" --format='value(email)'); then
    echo "error: cannot check whether $sa exists; refusing to continue." >&2
    exit 1
  fi
  if [ -n "$found" ]; then
    if ! members=$(gcloud iam service-accounts get-iam-policy "$sa" \
      --flatten='bindings[].members' --filter='bindings.role=roles/iam.workloadIdentityUser' \
      --format='value(bindings.members)'); then
      echo "error: cannot read the IAM policy of $sa; refusing to continue." >&2
      exit 1
    fi
    if grep -q "/workloadIdentityPools/github/subject/" <<<"$members"; then
      echo "error: $sa is bound to the production environment's subject (A6)." >&2
      echo "This M5 script would re-add the repository-wide binding and undo that" >&2
      echo "protection. Nothing was changed. Use 'bash scripts/gcp_setup_m8.sh status'" >&2
      echo "to inspect the identities; README → Production storage." >&2
      exit 1
    fi
  fi
done

gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com firebase.googleapis.com identitytoolkit.googleapis.com

# Firestore (Native mode). The location is permanent.
gcloud firestore databases create --location="$REGION" --type=firestore-native

# Container registry for the API image
gcloud artifacts repositories create chartlens --repository-format=docker \
  --location="$REGION" --description="ChartLens images"

# Runtime identity of the API: READ-ONLY on the lake, read/write on Firestore app state
gcloud iam service-accounts create chartlens-api --display-name="ChartLens API (Cloud Run)"
API_SA=chartlens-api@${PROJECT_ID}.iam.gserviceaccount.com
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$API_SA" --role="roles/storage.objectViewer"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$API_SA" --role="roles/datastore.user" --condition=None

# Deployer identity used by GitHub Actions (keyless, this repository only)
gcloud iam service-accounts create chartlens-deployer --display-name="ChartLens deployer (GitHub Actions)"
DEPLOY_SA=chartlens-deployer@${PROJECT_ID}.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$DEPLOY_SA" --role="roles/run.admin" --condition=None
gcloud artifacts repositories add-iam-policy-binding chartlens --location="$REGION" \
  --member="serviceAccount:$DEPLOY_SA" --role="roles/artifactregistry.writer"
gcloud iam service-accounts add-iam-policy-binding "$API_SA" \
  --member="serviceAccount:$DEPLOY_SA" --role="roles/iam.serviceAccountUser"
gcloud iam service-accounts add-iam-policy-binding "$DEPLOY_SA" \
  --member="$POOL_PRINCIPAL" --role="roles/iam.workloadIdentityUser"

echo
echo "Add these GitHub repository variables (Settings → Secrets and variables → Actions → Variables):"
echo "GCP_PROJECT_ID=$PROJECT_ID"
echo "GCP_REGION=$REGION"
echo "GCP_DEPLOY_SA=$DEPLOY_SA"
echo "GCP_API_SA=$API_SA"
echo "API_ADMIN_EMAILS=<your Google sign-in email>"
