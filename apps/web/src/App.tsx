import { type DragEvent, type FormEvent, useEffect, useState } from "react";
import { createJob, getJob, listJobs, uploadToPresignedUrl } from "./api";
import type { JobResponse, JobStatus } from "./types";

const TERMINAL_STATUSES = new Set<JobStatus>(["COMPLETED", "FAILED"]);
const STATUS_LABELS: Record<JobStatus, string> = {
  CREATED: "Created",
  UPLOADING: "Uploading",
  QUEUED: "Queued",
  PROCESSING: "Processing",
  INTERRUPTED: "Interrupted",
  FAILED: "Failed",
  COMPLETED: "Completed",
};

function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatTimestamp(iso: string): string {
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(new Date(iso));
}

function shortId(value: string): string {
  if (value.length <= 12) {
    return value;
  }
  return `${value.slice(0, 8)}…${value.slice(-4)}`;
}

function statusTone(status: JobStatus): string {
  switch (status) {
    case "COMPLETED":
      return "tone-success";
    case "PROCESSING":
      return "tone-cyan";
    case "QUEUED":
      return "tone-warm";
    case "UPLOADING":
      return "tone-blue";
    case "INTERRUPTED":
      return "tone-orange";
    case "FAILED":
      return "tone-red";
    case "CREATED":
    default:
      return "tone-neutral";
  }
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return "Something went wrong.";
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

export function App() {
  const [jobs, setJobs] = useState<JobResponse[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [detailJob, setDetailJob] = useState<JobResponse | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [uploadPhase, setUploadPhase] = useState("Idle");
  const [uploadProgress, setUploadProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [lastSyncedAt, setLastSyncedAt] = useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isDragging, setIsDragging] = useState(false);

  async function refreshJobs(): Promise<void> {
    setIsRefreshing(true);
    try {
      const nextJobs = await listJobs(25);
      setJobs(nextJobs);
      setLastSyncedAt(new Date().toISOString());
      if (!selectedJobId && nextJobs.length > 0) {
        setSelectedJobId(nextJobs[0].job_id);
      }
      if (selectedJobId) {
        const selected = nextJobs.find((job) => job.job_id === selectedJobId) ?? null;
        if (selected) {
          setDetailJob(selected);
        }
      }
    } catch (refreshError) {
      setError(getErrorMessage(refreshError));
    } finally {
      setIsRefreshing(false);
    }
  }

  useEffect(() => {
    void refreshJobs();
    const intervalId = window.setInterval(() => {
      void refreshJobs();
    }, 4000);
    return () => {
      window.clearInterval(intervalId);
    };
    // The interval intentionally uses the latest render's refresh function.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedJobId]);

  async function monitorJob(jobId: string): Promise<JobResponse> {
    for (let elapsed = 0; elapsed < 600000; elapsed += 2500) {
      const job = await getJob(jobId);
      setDetailJob(job);
      setSelectedJobId(jobId);
      if (TERMINAL_STATUSES.has(job.status)) {
        return job;
      }
      await sleep(2500);
    }
    throw new Error("Timed out waiting for the job to finish.");
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setError(null);

    if (!selectedFile) {
      setError("Choose a video file first.");
      return;
    }

    setIsSubmitting(true);
    setUploadProgress(0);

    try {
      setUploadPhase("Creating job record");
      const createdJob = await createJob(selectedFile);
      setSelectedJobId(createdJob.job_id);
      setDetailJob(null);
      await refreshJobs();

      setUploadPhase("Uploading source to S3");
      await uploadToPresignedUrl(createdJob.upload, selectedFile, (progress) => {
        setUploadProgress(progress);
      });

      setUploadPhase("Watching worker");
      const completedJob = await monitorJob(createdJob.job_id);
      setDetailJob(completedJob);
      await refreshJobs();
      setUploadPhase(`Job ${STATUS_LABELS[completedJob.status].toLowerCase()}`);
    } catch (submitError) {
      setError(getErrorMessage(submitError));
      setUploadPhase("Idle");
    } finally {
      setIsSubmitting(false);
      setUploadProgress(0);
    }
  }

  function handleDropZoneDragEnter(event: DragEvent<HTMLLabelElement>): void {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(true);
  }

  function handleDropZoneDragOver(event: DragEvent<HTMLLabelElement>): void {
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = "copy";
    setIsDragging(true);
  }

  function handleDropZoneDragLeave(event: DragEvent<HTMLLabelElement>): void {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(false);
  }

  function handleDropZoneDrop(event: DragEvent<HTMLLabelElement>): void {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(false);
    const droppedFile = event.dataTransfer.files?.[0] ?? null;
    if (droppedFile) {
      setSelectedFile(droppedFile);
      setError(null);
    }
  }

  const summary = jobs.reduce<Record<JobStatus, number>>(
    (accumulator, job) => {
      accumulator[job.status] += 1;
      return accumulator;
    },
    {
      CREATED: 0,
      UPLOADING: 0,
      QUEUED: 0,
      PROCESSING: 0,
      INTERRUPTED: 0,
      FAILED: 0,
      COMPLETED: 0,
    },
  );

  const activeJob = detailJob ?? jobs.find((job) => job.job_id === selectedJobId) ?? jobs[0] ?? null;

  return (
    <div className="shell">
      <div className="backdrop backdrop-one" />
      <div className="backdrop backdrop-two" />

      <header className="hero">
        <div>
          <p className="eyebrow">Elastic control surface</p>
          <h1>Watch uploads, queueing, and transcodes from one place.</h1>
          <p className="lede">
            This dashboard creates jobs, uploads the source video directly to S3 with a presigned URL,
            and keeps a live eye on the worker pipeline until the output lands.
          </p>
        </div>

        <div className="status-banner">
          <span className={`pill ${isRefreshing ? "tone-blue" : "tone-success"}`}>
            {isRefreshing ? "Refreshing" : "Live"}
          </span>
          <span>{lastSyncedAt ? `Synced ${formatTimestamp(lastSyncedAt)}` : "Waiting for first sync"}</span>
        </div>
      </header>

      <section className="metrics-grid">
        <article className="metric-card">
          <span className="metric-label">Jobs</span>
          <strong>{jobs.length}</strong>
        </article>
        <article className="metric-card">
          <span className="metric-label">Queued</span>
          <strong>{summary.QUEUED + summary.UPLOADING}</strong>
        </article>
        <article className="metric-card">
          <span className="metric-label">Processing</span>
          <strong>{summary.PROCESSING}</strong>
        </article>
        <article className="metric-card">
          <span className="metric-label">Completed</span>
          <strong>{summary.COMPLETED}</strong>
        </article>
      </section>

      <main className="workspace">
        <section className="panel panel-highlight">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">Upload</p>
              <h2>Send a video straight to S3</h2>
            </div>
            <span className="pill tone-neutral">Preset 1080p</span>
          </div>

          <form className="upload-form" onSubmit={handleSubmit}>
            <label
              className={`file-dropzone ${isDragging ? "dragging" : ""}`}
              onDragEnter={handleDropZoneDragEnter}
              onDragOver={handleDropZoneDragOver}
              onDragLeave={handleDropZoneDragLeave}
              onDrop={handleDropZoneDrop}
            >
              <input
                type="file"
                accept="video/*"
                onChange={(event) => {
                  setSelectedFile(event.target.files?.[0] ?? null);
                  setError(null);
                }}
              />
              <span className="drop-title">
                {selectedFile
                  ? selectedFile.name
                  : isDragging
                    ? "Drop the file to upload"
                    : "Choose a source video"}
              </span>
              <span className="drop-meta">
                {selectedFile
                  ? formatBytes(selectedFile.size)
                  : isDragging
                    ? "Release to attach the video to the job."
                    : "MP4, MOV, or another browser-friendly video file."}
              </span>
            </label>

            <div className="upload-status">
              <div className="upload-status-row">
                <span>{uploadPhase}</span>
                <span>{Math.round(uploadProgress * 100)}%</span>
              </div>
              <div className="progress-track" aria-hidden="true">
                <div className="progress-fill" style={{ width: `${uploadProgress * 100}%` }} />
              </div>
            </div>

            {error ? <div className="error-box">{error}</div> : null}

            <button className="primary-button" type="submit" disabled={isSubmitting || !selectedFile}>
              {isSubmitting ? "Working..." : "Upload & watch"}
            </button>
          </form>
        </section>

        <section className="panel">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">Recent jobs</p>
              <h2>Queue and transcode activity</h2>
            </div>
            <span className="pill tone-warm">{jobs.length} rows</span>
          </div>

          <div className="job-table">
            <div className="job-table-head">
              <span>Job</span>
              <span>Status</span>
              <span>Attempts</span>
              <span>Updated</span>
            </div>
            {jobs.length === 0 ? (
              <div className="empty-state">
                <h3>No jobs yet.</h3>
                <p>Upload a file to start the ingest pipeline and light up the table.</p>
              </div>
            ) : (
              jobs.map((job) => (
                <button
                  className={`job-row ${selectedJobId === job.job_id ? "selected" : ""}`}
                  key={job.job_id}
                  type="button"
                  onClick={() => {
                    setSelectedJobId(job.job_id);
                    setDetailJob(job);
                  }}
                >
                  <span className="job-cell mono">{shortId(job.job_id)}</span>
                  <span className="job-cell">
                    <span className={`pill ${statusTone(job.status)}`}>{STATUS_LABELS[job.status]}</span>
                  </span>
                  <span className="job-cell mono">{job.attempt_count}</span>
                  <span className="job-cell mono">{formatTimestamp(job.updated_at)}</span>
                </button>
              ))
            )}
          </div>
        </section>
      </main>

      <section className="panel detail-panel">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">Selected job</p>
            <h2>State ledger</h2>
          </div>
          {activeJob ? <span className={`pill ${statusTone(activeJob.status)}`}>{STATUS_LABELS[activeJob.status]}</span> : null}
        </div>

        {activeJob ? (
          <div className="detail-grid">
            <div className="detail-field">
              <span>Job ID</span>
              <strong className="mono">{activeJob.job_id}</strong>
            </div>
            <div className="detail-field">
              <span>Preset</span>
              <strong>{activeJob.preset}</strong>
            </div>
            <div className="detail-field">
              <span>Attempts</span>
              <strong>{activeJob.attempt_count}</strong>
            </div>
            <div className="detail-field">
              <span>Created</span>
              <strong>{formatTimestamp(activeJob.created_at)}</strong>
            </div>
            <div className="detail-field detail-span">
              <span>Input key</span>
              <strong className="mono">{activeJob.input_key}</strong>
            </div>
            <div className="detail-field detail-span">
              <span>Output key</span>
              <strong className="mono">{activeJob.output_key}</strong>
            </div>
            <div className="detail-field detail-span">
              <span>Last error</span>
              <strong className={activeJob.last_error ? "error-text" : "muted"}>
                {activeJob.last_error ?? "None"}
              </strong>
            </div>
          </div>
        ) : (
          <div className="empty-state compact">
            <h3>No job selected.</h3>
            <p>Click a job in the table or upload a file to inspect the live job record here.</p>
          </div>
        )}
      </section>
    </div>
  );
}
