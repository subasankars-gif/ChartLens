#!/usr/bin/env bash
# Milestone 5 one-time setup (ADR-0016). Run in Cloud Shell as the project owner.
# Idempotent where gcloud allows: re-running prints "already exists" errors and carries on.
#
# Before running: add Firebase to the project and enable Google sign-in (console steps
# in the README, "API deployment"). This script does the rest.
set -u
PROJECT_ID=chartlens-lake-13934
PROJECT_NUMBER=1082278531047
REGION=${REGION:?set REGION to the bucket location first: gcloud storage buckets describe gs://chartlens-lake-13934-data --format="value(location)"}
BUCKET=chartlens-lake-13934-data
REPO=subasankars-gif/ChartLens
POOL_PRINCIPAL="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github/attribute.repository/${REPO}"

gcloud config set project "$PROJECT_ID"
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
