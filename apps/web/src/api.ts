import type {
  CreateJobRequest,
  CreateJobResponse,
  JobResponse,
  MultipartUploadInstructions,
  UploadInstructions,
} from "./types";

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

function uploadPart(url: string, blob: Blob, onBytes: (loaded: number) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onBytes(event.loaded);
      }
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        const etag = xhr.getResponseHeader("ETag");
        if (!etag) {
          reject(new Error("S3 did not return an ETag for an uploaded part."));
          return;
        }
        resolve(etag);
        return;
      }
      reject(new Error(`Part upload failed with status ${xhr.status}.`));
    };

    xhr.onerror = () => reject(new Error("Part upload failed due to a network error."));
    xhr.send(blob);
  });
}

async function uploadMultipart(
  multipart: MultipartUploadInstructions,
  file: File,
  onProgress?: (progress: number) => void,
): Promise<void> {
  const completedParts: { part_number: number; etag: string }[] = [];
  const bytesPerPart = new Map<number, number>();

  const reportProgress = () => {
    if (!onProgress) {
      return;
    }
    let uploaded = 0;
    for (const bytes of bytesPerPart.values()) {
      uploaded += bytes;
    }
    onProgress(Math.min(uploaded / file.size, 1));
  };

  for (const part of multipart.parts) {
    const start = (part.part_number - 1) * multipart.part_size_bytes;
    const blob = file.slice(start, Math.min(start + multipart.part_size_bytes, file.size));
    const etag = await uploadPart(part.url, blob, (loaded) => {
      bytesPerPart.set(part.part_number, loaded);
      reportProgress();
    });
    bytesPerPart.set(part.part_number, blob.size);
    reportProgress();
    completedParts.push({ part_number: part.part_number, etag });
  }

  const response = await fetch(buildApiUrl(multipart.complete_path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ upload_id: multipart.upload_id, parts: completedParts }),
  });
  await parseJsonResponse<JobResponse>(response);
}

export async function uploadSource(
  job: CreateJobResponse,
  file: File,
  onProgress?: (progress: number) => void,
): Promise<void> {
  if (job.multipart_upload) {
    await uploadMultipart(job.multipart_upload, file, onProgress);
    return;
  }
  if (job.upload) {
    await uploadToPresignedUrl(job.upload, file, onProgress);
    return;
  }
  throw new Error("Job response contained no upload instructions.");
}
