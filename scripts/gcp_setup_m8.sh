#!/usr/bin/env bash
# M8 completion gate, A6: only `main`'s current workflows may reach the live lake or
# deploy. Run in Cloud Shell as the project owner, in two steps around the M8 merge.
#
#   bash scripts/gcp_setup_m8.sh status        # print the provider and both bindings; change nothing
#   bash scripts/gcp_setup_m8.sh before-merge  # ADD the environment-subject binding (harmless today)
#   bash scripts/gcp_setup_m8.sh after-merge   # REMOVE every other pool binding (repository-wide, stale subjects)
#
# Why two steps: today's `main` workflows do not name the `production` environment, so
# their OIDC subject is `repo:…:ref:refs/heads/main`. Removing the repository-wide
# binding before the merge would stop the current production refresh. After the merge,
# every workflow on `main` that may reach GCP names the environment, and the subject
# becomes `repo:…@<ids>:environment:production`, which GitHub issues only to jobs the
# environment admits (`main`, see README → Production storage).
#
# Effect after both steps: the pipeline and deployer identities accept exactly one
# subject. A re-run of a pre-merge workflow, or any workflow on another branch, cannot
# impersonate them, whatever variables it is given.
set -u
PROJECT_ID=chartlens-lake-13934
PROJECT_NUMBER=1082278531047
POOL=github
# GitHub issues this repository immutable subject claims (owner and repository IDs after
# the names; the repository was renamed after 2026-07-15, which adopts the format). The
# A6 probe recorded a real token's subject: repo:subasankars-gif@288858503/
# ChartLens@1398125563:ref:refs/heads/probe/a6. For a job in the environment the suffix
# is :environment:production.
SUBJECT="repo:subasankars-gif@288858503/ChartLens@1398125563:environment:production"
ENV_PRINCIPAL="principal://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/subject/${SUBJECT}"
POOL_MEMBER_RE="^principal(Set)?://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL}/"
SAS=(
  "chartlens-pipeline@${PROJECT_ID}.iam.gserviceaccount.com"
  "chartlens-deployer@${PROJECT_ID}.iam.gserviceaccount.com"
)
ROLE=roles/iam.workloadIdentityUser
MODE=${1:-status}

gcloud config set project "$PROJECT_ID" >/dev/null

show() {
  echo "== Workload Identity provider(s) in pool '$POOL'"
  gcloud iam workload-identity-pools providers list --location=global \
    --workload-identity-pool="$POOL" \
    --format='table(name.basename(), attributeMapping, attributeCondition)'
  for sa in "${SAS[@]}"; do
    echo "== $ROLE on $sa"
    gcloud iam service-accounts get-iam-policy "$sa" \
      --flatten='bindings[].members' --filter="bindings.role=$ROLE" \
      --format='value(bindings.members)'
  done
}

subject_is_mapped() {
  gcloud iam workload-identity-pools providers list --location=global \
    --workload-identity-pool="$POOL" --format='value(attributeMapping)' \
    | grep -q "google.subject=assertion.sub"
}

case "$MODE" in
  status)
    show
    ;;
  before-merge)
    if ! subject_is_mapped; then
      echo "error: the provider does not map google.subject=assertion.sub;" >&2
      echo "the environment-subject binding would never match. Nothing changed." >&2
      exit 1
    fi
    for sa in "${SAS[@]}"; do
      gcloud iam service-accounts add-iam-policy-binding "$sa" \
        --member="$ENV_PRINCIPAL" --role="$ROLE" --condition=None >/dev/null
      echo "added: $ENV_PRINCIPAL → $sa"
    done
    show
    ;;
  after-merge)
    for sa in "${SAS[@]}"; do
      gcloud iam service-accounts get-iam-policy "$sa" \
        --flatten='bindings[].members' --filter="bindings.role=$ROLE" \
        --format='value(bindings.members)' | grep -qx "$ENV_PRINCIPAL" || {
          echo "error: $sa has no environment-subject binding; run before-merge first." >&2
          exit 1
        }
    done
    for sa in "${SAS[@]}"; do
      gcloud iam service-accounts get-iam-policy "$sa" \
        --flatten='bindings[].members' --filter="bindings.role=$ROLE" \
        --format='value(bindings.members)' | grep -E "$POOL_MEMBER_RE" | grep -vx "$ENV_PRINCIPAL" \
        | while read -r member; do
          gcloud iam service-accounts remove-iam-policy-binding "$sa" \
            --member="$member" --role="$ROLE" >/dev/null
          echo "removed: $member → $sa"
        done
    done
    show
    echo
    echo "Expected now: exactly one member per identity, $ENV_PRINCIPAL"
    ;;
  *)
    echo "usage: $0 status|before-merge|after-merge" >&2
    exit 2
    ;;
esac
