# What Changed And Why

This document walks through the distributed-systems upgrade applied on
2026-07-06, written to be read top to bottom. Each section explains the
problem, the mechanism that fixes it, and where the code lives. These are the
concepts an interviewer will drill into, so the "why" matters more than the
diff.

Everything below was verified: all 45 tests pass, and
`scripts/chaos_demo.py` was run against LocalStack in both modes —
a SIGKILLed worker's job was stolen and completed by a second worker
(attempt 1 → 2), and a SIGTERMed worker handed its job off in about a second.

---

## 1. The bug that motivated everything: the stranded `PROCESSING` job

**The problem.** The old worker logic said: *if the job is already
`PROCESSING`, this is a duplicate event — skip it and delete the message.*
That is correct while the first worker is alive. But if a worker is killed
without warning (SIGKILL, out-of-memory kill, instant Spot reclaim — cases
where the SIGTERM handler never runs), the job stays `PROCESSING` in DynamoDB
forever. When SQS redelivers the message, the next worker sees `PROCESSING`,
assumes someone is working on it, and **deletes the only trigger that could
ever retry the job**. The job is permanently stuck, and nothing notices.

The fix has three cooperating parts: **leases**, **fencing tokens**, and a
**reconciler**. This trio is the textbook solution (Kleppmann, *Designing
Data-Intensive Applications*, chapter 8) and is the core of what makes this
project a distributed-systems demo.

### 1a. Leases — ownership that expires (`apps/api/app/store.py`)

A job record now carries `lease_owner` (which pod owns the attempt) and
`lease_expires_at` (when that ownership lapses). Claiming a job is a single
DynamoDB `UpdateItem` with a condition:

```
status = QUEUED
OR (status = PROCESSING AND lease_expires_at <= now)
```

DynamoDB evaluates the condition and applies the write **atomically**, so two
workers racing for the same job can never both win — the loser gets a
`ConditionalCheckFailedException` and walks away. A live worker renews its
lease every 15 seconds (same heartbeat that extends SQS visibility); a dead
worker stops renewing, its lease expires, and the next redelivery **steals**
the job. This is why the SIGKILL demo recovers with no human intervention.

Detail worth knowing: `lease_expires_at` is stored as an **epoch number**,
not an ISO string, because the `<=` inside the DynamoDB condition must be a
numeric comparison. String-comparing timestamps is a classic latent bug.

### 1b. Fencing tokens — locking zombies out (`store.py`, `service.py`)

Leases create a new hazard: the "dead" worker might not be dead — just
paused, GC-stalled, or partitioned — and wake up after its job was stolen. If
it then wrote `COMPLETED`, it would corrupt the new owner's attempt.

The fix: **every claim increments `attempt_count`, and every subsequent write
by that worker is conditioned on `attempt_count` still matching its claim.**
The stale worker holds attempt 1; the thief advanced it to 2; every write the
zombie tries (lease renewal, completion, failure) fails its condition. The
attempt count *is* the fencing token. On the first failed heartbeat the
zombie also cancels its own ffmpeg (`lease_lost` event), so it stops burning
CPU on work it can no longer commit.

The output artifact deliberately does not need fencing: output keys are
deterministic and every attempt transcodes the same source, so a late
overwrite is content-equivalent. The DynamoDB job record is the single
authority on completion. (Being able to explain *why* you didn't fence the
artifact is as valuable as the fencing itself.)

### 1c. Message handling is now three-way, not two-way (`apps/worker/app/service.py`)

The worker used to answer one question per message: delete or not. Now it
distinguishes:

- **DELETE** — job finished or event provably stale; consume the message.
- **RELEASE** — attempt ended without a verdict (shutdown, retryable
  failure); set visibility to 0 so another worker retries immediately.
- **LEAVE** — someone else actively owns the job. Don't delete (that was the
  original bug) and don't release (that would redeliver instantly and
  hot-loop). Let the visibility timeout re-surface the message later as a
  natural re-check.

## 2. The reconciler — jobs can no longer get stuck (`apps/api/app/reconciler.py`)

Queues guarantee delivery of messages that exist. They do **not** guarantee a
message was ever produced (S3 notification misconfigured or lost) or still
exists (dead-lettered after repeated failures). Any of those left a job stuck
in `UPLOADING` or `QUEUED` forever with nothing watching.

The API (which, unlike workers, never scales to zero) now runs a background
sweep every `ELASTIC_RECONCILER_INTERVAL_SECONDS` (60s default):

| Stuck state | Detection | Action |
|---|---|---|
| `UPLOADING` too long, object **is** in S3 | `head_object` | move to `QUEUED`, inject a synthetic requeue message |
| `UPLOADING` too long, object never arrived | age > `upload_expiry_seconds` | fail the job |
| `PROCESSING`, lease expired, no progress | conditional write requiring expired lease | reset to `INTERRUPTED`, requeue |
| `QUEUED`/`INTERRUPTED` idle (message lost/DLQ'd) | age > `stale_queued_seconds` | requeue, bump `updated_at` so one sweep doesn't spam every cycle |

Two design points interviewers like: (1) the reconciler uses the *same
conditional writes* as workers, so a race between the sweeper and a live
worker always resolves in the worker's favor — the lease-expiry condition
makes preempting an active worker impossible; (2) "janitor processes" are how
real systems stay honest about the difference between *durable buffering*
and *guaranteed triggering*.

The worker understands the synthetic message shape
(`{"elastic_requeue": {...}}`) in `normalize_s3_event_message`.

## 3. The large-file story is now true

- **Worker streaming** (`service.py`): `get_object` + `Body.read()` (whole
  video in RAM — a 4 GB file needed 4 GB of pod memory) became
  `download_file`/`upload_file`, boto3's managed transfer that streams to
  disk in chunks and uses multipart under the hood. Memory is now flat
  regardless of file size.
- **Multipart presigned upload** (`main.py`, dashboard): a single presigned
  PUT caps at 5 GiB and can't resume. At or above
  `ELASTIC_MULTIPART_THRESHOLD_BYTES` (100 MiB) the API now returns a
  multipart plan instead — an S3 `UploadId`, per-part presigned URLs (part
  size respects S3's 5 MiB minimum and 10,000-part maximum), and
  complete/abort endpoints. The dashboard slices the file, uploads parts,
  collects the `ETag` from each response, and posts them back to
  `/jobs/{id}/uploads/complete`, where the API calls S3
  `CompleteMultipartUpload`. S3 then emits `ObjectCreated:CompleteMultipartUpload`
  and the pipeline proceeds identically. Verified against LocalStack with a
  120 MiB two-part upload.
- **Enforced Content-Length** (`main.py`): the single-PUT presigned URL now
  signs `ContentLength`, so S3 rejects any upload whose byte count differs
  from the `size_bytes` declared at job creation. The declared size became a
  contract instead of a suggestion.
- **Upload cap**: requests above `ELASTIC_MAX_UPLOAD_BYTES` (50 GiB) get a
  413 instead of an upload that can never work.

## 4. ffmpeg failure is now failure

The old worker silently **copied the source file to the output key and marked
the job COMPLETED** when ffmpeg failed or was missing. That's a correctness
violation wearing a robustness costume — the client sees `COMPLETED` and
downloads a non-transcoded file. Now a transcode failure raises
`TranscodeError`, and:

- attempts below `ELASTIC_MAX_ATTEMPTS` (3) → job goes `INTERRUPTED`,
  message released, another worker retries (covers transient causes);
- the final attempt → job goes `FAILED` with the ffmpeg stderr tail in
  `last_error`, message deleted. The SQS DLQ (`maxReceiveCount` 5) remains
  as the backstop beneath the app-level policy.

## 5. DynamoDB access patterns: GSI instead of Scan

`list_jobs` did a full-table `Scan` with `ConsistentRead` on every dashboard
poll — O(entire table) forever, no pagination. The table now has a global
secondary index `status-updated_at-index` (partition key `status`, sort key
`updated_at`), so:

- the dashboard's "recent jobs" is a `Query` per status merged client-side
  (and `GET /jobs?status=PROCESSING` filters server-side);
- the reconciler's staleness sweeps are range queries
  (`status = X AND updated_at <= cutoff`) — exactly the access pattern the
  index was designed around, which is how DynamoDB modeling is supposed to
  work (design the index from the query, not the other way).

The GSI is added in three places: Terraform (`storage.tf`) for AWS, the
LocalStack bootstrap (which also upgrades an existing local table in place
via `update_table`), and both store implementations.

## 6. Observability: from `print` to evidence

- **Structured logs** (`apps/common/obs.py`): every log line can carry
  `job_id`, `attempt`, `worker_id`, `sqs_message_id`. With
  `ELASTIC_LOG_JSON=true` (default in Kubernetes) lines are JSON, so
  `kubectl logs | jq 'select(.job_id=="...")'` reconstructs one job's history
  across every pod that touched it.
- **Prometheus metrics** (`apps/common/metrics.py`): worker exports on :9100,
  API at `/metrics`. The interesting ones are the resilience counters —
  `elastic_lease_steals_total`, `elastic_lease_renewal_failures_total`,
  `elastic_jobs_interrupted_total`, `elastic_reconciler_requeued_total{reason}` —
  plus `elastic_transcode_duration_seconds` as a histogram.
- `ELASTIC_WORKER_ID` is injected from the pod name via the Kubernetes
  downward API, so `lease_owner` in DynamoDB literally names the pod holding
  each job.

## 7. Small latent bugs fixed

- **S3 event keys are URL-encoded** (spaces arrive as `+`). The worker now
  `unquote_plus`es keys; previously a filename with a space would have failed
  the key match and dropped the event.
- **Duplicate SQS delivery during active processing** no longer deletes the
  message (see §1c LEAVE).
- **Shutdown before claim** just releases the message instead of writing an
  `INTERRUPTED` state for an attempt that never started.

## 8. Fault-injection demo (`scripts/chaos_demo.py`)

The resume-grade move: don't claim resilience, demonstrate it.

```bash
uv run python scripts/dev_up.py          # LocalStack + API (demo spawns its own workers)
uv run python scripts/chaos_demo.py --mode sigkill
uv run python scripts/chaos_demo.py --mode sigterm
```

`sigkill` shows the ugly path: worker dies with zero cleanup, job stranded in
`PROCESSING` under a dead owner, then stolen after lease expiry
(`lease_owner` visibly changes hands, attempt 1 → 2) and completed.
`sigterm` shows the graceful Spot path: `INTERRUPTED` + immediate release,
recovery in ~1 second. The measured contrast between the two is itself a
talking point: graceful shutdown buys you ~30 seconds of recovery latency.

## 9. Tests (45 passing)

New coverage that maps one-to-one to the failure model:

- `tests/test_lease_store.py` — claim, claim-rejected-while-leased, steal
  after expiry, zombie renewal rejected, fenced completion rejected, the
  reconciler's lease-expiry guard, staleness queries.
- `tests/test_reconciler.py` — every sweep case, including "live long
  transcode is untouchable" and "one sweep doesn't resend twice".
- `tests/test_worker.py` — rewritten: duplicate-delivery LEAVE, lease steal
  end-to-end, bounded retries, final-attempt failure, URL-decoding, synthetic
  requeue parsing.
- `tests/test_api_jobs.py` — multipart threshold switch, part assembly
  ordering, oversize rejection.

## 10. Deployed to AWS (2026-07-07)

The release is live in the dev environment (image tag `lease-20260706`):
Terraform applied the jobs-table GSI and IAM policy updates in place
(0 add / 3 change / 0 destroy), images were rebuilt for `linux/amd64` and
pushed to ECR under a unique tag (required because `imagePullPolicy:
IfNotPresent` would otherwise serve the cached old image), and the rendered
overlay was re-applied. The full deployment log and on-AWS fault-injection
results — including a force-deleted pod's job being rescued by the
reconciler and a 9-second graceful handoff — are in
[`docs/aws-validation.md`](docs/aws-validation.md).

New dependency: `prometheus-client` (already in `pyproject.toml`/`uv.lock`).

## Interview cheat-sheet (the one-liners)

- *"How do you know another worker isn't already on it?"* — Lease in
  DynamoDB, renewed on the visibility heartbeat, claimable only via an atomic
  conditional write when expired.
- *"What if the 'dead' worker comes back?"* — It carries a stale attempt
  number; every write it tries is fenced out, and lease-loss cancels its
  ffmpeg.
- *"What if the S3 event never arrives?"* — Reconciler head-checks S3 for
  stale `UPLOADING` jobs and injects a synthetic requeue message.
- *"Why not exactly-once?"* — Can't have it end-to-end; instead: at-least-once
  execution + conditional-write state machine + deterministic output keys =
  idempotent from the client's view.
- *"How do you handle a 10 GB file?"* — Never through the API: presigned
  multipart upload straight to S3; workers stream to disk, memory stays flat.
