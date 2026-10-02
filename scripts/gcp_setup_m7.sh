#!/usr/bin/env bash
# Milestone 7 one-time setup (ADR-0018). Run in Cloud Shell as the project owner,
# BEFORE merging M7: the production refresh records its runs in Firestore from the first
# scheduled run after the merge.
#
#   bash scripts/gcp_setup_m7.sh                      # step 1 only (Firestore access)
#   bash scripts/gcp_setup_m7.sh ~/chartlens-app.pem  # also store the GitHub App key
#
# Idempotent where gcloud allows: re-running prints "already exists" and carries on.
# The key file is read once into Secret Manager; delete your copy afterwards.
set -u
PROJECT_ID=chartlens-lake-13934
PIPELINE_SA=chartlens-pipeline@${PROJECT_ID}.iam.gserviceaccount.com
API_SA=chartlens-api@${PROJECT_ID}.iam.gserviceaccount.com
SECRET=chartlens-github-app-key
KEY_FILE=${1:-}

gcloud config set project "$PROJECT_ID"

# 1. The pipeline writes run records and snapshot history (operational metadata only).
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$PIPELINE_SA" --role="roles/datastore.user" --condition=None

# 2. The GitHub App's private key, readable by the API's identity and nobody else.
if [ -n "$KEY_FILE" ]; then
  if ! grep -q "PRIVATE KEY" "$KEY_FILE"; then
    echo "error: $KEY_FILE does not look like a PEM private key" >&2
    exit 1
  fi
  gcloud services enable secretmanager.googleapis.com
  gcloud secrets create "$SECRET" --replication-policy=automatic 2>/dev/null \
    || echo "secret $SECRET already exists; adding a new version"
  gcloud secrets versions add "$SECRET" --data-file="$KEY_FILE"
  gcloud secrets add-iam-policy-binding "$SECRET" \
    --member="serviceAccount:$API_SA" --role="roles/secretmanager.secretAccessor"
  echo
  echo "Stored. Now delete the local key file: rm '$KEY_FILE'"
fi

echo
echo "Then add the repository variable CHARTLENS_GH_APP_ID (the App ID) in GitHub."
