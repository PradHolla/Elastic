# AWS Validation Report — Lease/Reconciler Release (2026-07-07)

This documents the deployment of the resilience upgrade (see `CHANGES.md`) to
the live dev environment and the fault-injection tests run against it. All
outputs below are real, captured from account `<ACCOUNT_ID>`, cluster
`elastic-dev-eks`, region `us-east-1`. Image tag: `lease-20260706`.

## Cost Note

The release added **no compute resources**: no new nodes, node-group sizes
unchanged, KEDA max replicas still 5 (pods scale on the existing Spot node).
New billable items are the DynamoDB GSI on a PAY_PER_REQUEST table
(kilobytes of storage, per-request pricing on a near-empty table) and the
reconciler's ~5 small queries per minute — cents per month combined. The
dominant costs (EKS control plane, node hours) are exactly what they were.

## Deployment Steps Performed

1. **Terraform** (`infra/terraform/envs/dev`): plan showed
   `0 to add, 3 to change, 0 to destroy` — jobs-table GSI, API role policy,
   worker role policy (recompute-only, statements identical). Applied.
   The first apply was interrupted by a local machine freeze after the GSI
   update started; the stale state lock was released with
   `terraform force-unlock` (no terraform process running) and a fresh plan
   (`0 add / 1 change / 0 destroy`) landed the remaining IAM change.

   ```
   $ aws dynamodb describe-table ... → "status-updated_at-index": ACTIVE
   $ aws iam get-role-policy ... → Sids: JobsTableAccess, PresignedUploadAccess, ReconcilerQueueAccess
   ```

2. **Images**: `scripts/build_push_ecr.py` built api/worker/web for
   `linux/amd64` and pushed `:dev`; images were re-tagged and pushed as
   `:lease-20260706` (same digests). The unique tag matters because the
   deployments use `imagePullPolicy: IfNotPresent` — re-using `:dev` would
   have let the node serve the cached old image.

3. **Kubernetes**: `scripts/render_aws_overlay.py --image-tag lease-20260706`,
   then `kubectl apply -k infra/k8s/overlays/aws-generated`. The merged
   ConfigMap picked up the new keys (`ELASTIC_LOG_JSON`,
   `ELASTIC_METRICS_ENABLED`, `ELASTIC_RECONCILER_ENABLED`,
   `ELASTIC_LEASE_DURATION_SECONDS=90`, `ELASTIC_MAX_ATTEMPTS=3`), the worker
   got `ELASTIC_WORKER_ID` from the pod name (downward API) and metrics port
   9100. Rollout completed; the new API pod's first JSON log line:

   ```json
   {"ts": "2026-07-07T03:58:25.509901+00:00", "level": "INFO",
    "logger": "elastic.reconciler", "message": "reconciler started", "interval_seconds": 60}
   ```

## Test 1 — Happy Path + KEDA Scale-From-Zero ✅

Worker deployment was at 0 replicas. Job created through the port-forwarded
API; fixture uploaded via the presigned URL (signed Content-Length).

```
[00:00:04] created job b1b08159...
[00:00:07] uploaded fixture to S3 via presigned URL
[00:00:07] status=UPLOADING attempt=0
[00:00:53] status=PROCESSING attempt=1 lease_owner=elastic-worker-547b7794b8-f2m7w
[00:01:31] status=COMPLETED attempt=1
```

KEDA scaled 0→1 (~46s including pod schedule + image pull), the pod claimed
the lease under its own pod name, transcoded (37.6s per the histogram), and
completed. KEDA scaled back to 0 after the cooldown.

## Test 2 — Hard Crash (SIGKILL): Lease Steal / Reconciler Reset ✅

`kubectl delete pod --grace-period=0 --force` mid-transcode — no SIGTERM
handler runs; this is the failure mode that permanently stranded jobs in the
old code.

```
[00:01:53] status=PROCESSING attempt=1 lease_owner=elastic-worker-547b7794b8-f2m7w
           ← pod force-deleted here
[00:04:26] status=PROCESSING attempt=2 lease_owner=elastic-worker-547b7794b8-msfng
[00:05:04] status=COMPLETED attempt=2
```

Mid-outage forensics via the new GSI (job owned by a pod that no longer
exists, replacement already running):

```
$ aws dynamodb query --index-name status-updated_at-index --key ... status=PROCESSING
{ "job": "04f5d442...", "owner": "elastic-worker-547b7794b8-f2m7w", "attempt": "1" }
$ kubectl get pods → elastic-worker-547b7794b8-msfng   Running   15s
```

Recovery was driven by the **reconciler** (its 60s sweep beat the 120s SQS
visibility redelivery):

```json
{"level": "WARNING", "logger": "elastic.reconciler",
 "message": "reset job with expired lease",
 "job_id": "04f5d442...", "previous_owner": "elastic-worker-547b7794b8-f2m7w"}
{"message": "reconciler sweep finished", "requeued": 1, "expired": 0, "reset": 0}
```

The replacement pod claimed attempt 2 one second after the requeue. Total
recovery: ~2.5 minutes, zero human intervention. (In the LocalStack run of
`scripts/chaos_demo.py` the *redelivery/steal* path won instead — both
recovery layers are now individually proven.)

The final artifact was downloaded from S3 and verified with ffprobe:
`h264, 1920x1080` + `aac`.

## Test 3 — Graceful Drain (SIGTERM) ✅

Plain `kubectl delete pod` (120s grace) — the Spot-interruption shape where
the node gives notice.

```
[00:05:47] status=PROCESSING attempt=1 lease_owner=elastic-worker-547b7794b8-z4zn6
           ← pod deleted gracefully here
[00:05:56] status=PROCESSING attempt=2 lease_owner=elastic-worker-547b7794b8-msfng
[00:06:35] status=COMPLETED attempt=2
```

**9 seconds** from kill to re-claim: the SIGTERM handler stopped ffmpeg,
marked the job `INTERRUPTED`, and set the message's visibility to 0 so the
other pod picked it up immediately. Contrast with ~2.5 minutes for the hard
crash — graceful shutdown buys ~30s–2min of recovery latency, which is the
whole point of handling SIGTERM well on Spot.

## Test 4 — Observability ✅

Worker metrics (port-forward :9100, after the chaos rescue):

```
elastic_jobs_completed_total 1.0
elastic_lease_claims_total 2.0
elastic_visibility_extensions_total 4.0
elastic_transcode_duration_seconds_sum 37.57
```

API metrics (`/metrics/` — note the ASGI mount 307-redirects `/metrics`):

```
elastic_reconciler_requeued_total{reason="lease-expired"} 1.0
elastic_reconciler_expired_total 0.0
```

## Test 5 — Steady State After Chaos ✅

- Queue and DLQ both at 0 messages — nothing lost, nothing dead-lettered
  across two mid-transcode kills.
- Worker pods back at 0 (KEDA cooldown) — no idle compute burning.
- GSI-backed `GET /jobs?status=COMPLETED` lists all three test jobs with the
  expected attempt counts (1, 2, 2).

## Test 6 — Burst Load: KEDA Scale-Out Under Node Capacity Limits ✅

Six jobs created and uploaded in parallel (14s total) against a fleet at 0
workers. Capacity math beforehand: the single m5.large has 1930m allocatable
CPU with 800m already requested by api/web/system pods, leaving ~1130m — so
only **2** workers (500m requests each) can be scheduled, even though KEDA
(queueLength=1, max 5) will ask for 5 replicas for 6 messages.

Observed timeline (5s resolution):

```
[t+ 14s] all 6 uploads done
[t+ 22s] queue visible=6            | pods running=0 pending=0 | 6× UPLOADING
[t+ 35s] queue visible=5            | pods running=1 pending=0 | 1× PROCESSING
[t+ 55s] queue visible=5 inflight=1 | pods running=2 pending=3 | 2× PROCESSING
[t+116s]                              ...                      | 1× COMPLETED
[t+307s] queue visible=0 inflight=0 | pods running=2 pending=3 | 6× COMPLETED
[t+393s] queue visible=0            | pods running=0 pending=0 | 6× COMPLETED
```

What this demonstrates:

- **KEDA scaled 0 → 5 desired replicas** off queue depth; first worker was
  processing 35s after the burst began.
- **The node capped placement at 2 Running; 3 sat Pending** — exactly the
  predicted 1130m/500m. KEDA scales the *deployment*; the scheduler places
  pods only where CPU requests fit; there is no cluster autoscaler in this
  dev environment, deliberately (adding nodes is the cost lever).
- **The queue absorbed the difference.** Two concurrent workers drained six
  jobs in ~5 minutes; nothing failed, nothing dead-lettered, every job
  completed on attempt 1. Backpressure showed up as queue latency, not as
  errors — which is the correct failure mode for burst load.
- **Scale-in worked**: cooldown (60s) after the queue emptied brought the
  fleet back to 0, including the never-scheduled Pending pods.
- Jobs show `UPLOADING` until a worker first touches their event — the
  `UPLOADING → QUEUED → PROCESSING` hop happens at claim time, so `QUEUED`
  is often too brief to observe at 5s resolution.

Cost note for this test: zero additional infrastructure — the Pending pods
consume no compute, and the burst ran entirely on the existing node.

## Verdict

Every mechanism from `CHANGES.md` is deployed and demonstrated on AWS:
lease claim/steal, fencing (attempt advanced 1→2 on both takeovers),
reconciler reset + synthetic requeue, graceful-interrupt fast path, KEDA
scale 0→1→0 and burst scale-out 0→5-desired/2-schedulable→0, GSI queries,
JSON structured logs, and Prometheus metrics — with no cost-relevant
infrastructure added.
