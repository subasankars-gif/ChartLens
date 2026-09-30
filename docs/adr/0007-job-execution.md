# ADR-0007: Batch jobs on GitHub-hosted runners behind a CLI

**Status:** Accepted · 2026-09-30

## Decision

* Every batch job is a `chartlens-pipeline` CLI command. Workflows only choose the
  command and its arguments; they contain no pipeline logic.
* One workflow (`.github/workflows/pipeline-job.yml`) runs any job via
  `workflow_dispatch` inputs. The API's refresh endpoint (Milestone 7) triggers it
  through the GitHub REST API, so the UI never runs long work in a request.
* Jobs share a concurrency group, so two jobs never write to the lake at once.
* GitHub Actions authenticates to GCP with Workload Identity Federation — no
  long-lived service-account keys in repository secrets.
* Inputs reach the shell through environment variables, never by expression
  interpolation, so a crafted input cannot inject commands.

## Known risk

NSE is widely reported to block requests from cloud/datacenter IP ranges, which
include GitHub-hosted runners. Milestone 2 starts with a connectivity probe from a
hosted runner, before building on the assumption that it works.

If hosted runners are blocked, the fallbacks in order of effort are:

1. a self-hosted runner on a machine with a residential or India-based IP;
2. a small VM in an India GCP region running the same container on a schedule.

Because jobs are container + CLI, either fallback changes where the job runs, not
the code. The same property allows a later move to Cloud Run Jobs.

## Consequences

* GitHub Actions' free minutes for private repositories are capped; a 20-year
  backfill should run once, in chunks, rather than on every change.
* Job tracking (spec §42) is written by the CLI itself, so it is identical wherever
  the job runs.
