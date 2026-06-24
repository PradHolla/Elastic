import type { CreateJobRequest, CreateJobResponse, JobResponse, UploadInstructions } from "./types";

const DEFAULT_API_BASE_URL = "/api";

function getApiBaseUrl(): string {
  return (import.meta.env.VITE_API_BASE_URL as string | undefined)?.trim() || DEFAULT_API_BASE_URL;
}

function buildApiUrl(path: string): string {
  const base = getApiBaseUrl().replace(/\/+$/, "");
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  if (base.startsWith("http://") || base.startsWith("https://")) {
    return new URL(normalizedPath, `${base}/`).toString();
  }
  return `${base}${normalizedPath}`;
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `Request failed with status ${response.status}.`);
  }
  return (await response.json()) as T;
}

export async function listJobs(limit = 25): Promise<JobResponse[]> {
  const response = await fetch(buildApiUrl(`/jobs?limit=${limit}`));
  return parseJsonResponse<JobResponse[]>(response);
}

export async function getJob(jobId: string): Promise<JobResponse> {
  const response = await fetch(buildApiUrl(`/jobs/${jobId}`));
  return parseJsonResponse<JobResponse>(response);
}

export async function createJob(file: File): Promise<CreateJobResponse> {
  const payload: CreateJobRequest = {
    filename: file.name,
    content_type: file.type || "application/octet-stream",
    size_bytes: file.size,
    preset: "1080p",
  };

  const response = await fetch(buildApiUrl("/jobs"), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  return parseJsonResponse<CreateJobResponse>(response);
}

export function uploadToPresignedUrl(
  upload: UploadInstructions,
  file: File,
  onProgress?: (progress: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open(upload.method, upload.url);

    for (const [header, value] of Object.entries(upload.headers)) {
      xhr.setRequestHeader(header, value);
    }

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) {
        onProgress(event.loaded / event.total);
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve();
        return;
      }
      reject(new Error(`Upload failed with status ${xhr.status}.`));
    };

    xhr.onerror = () => reject(new Error("Upload failed due to a network error."));
    xhr.send(file);
  });
}
