export type JobStatus =
  | "CREATED"
  | "UPLOADING"
  | "QUEUED"
  | "PROCESSING"
  | "INTERRUPTED"
  | "FAILED"
  | "COMPLETED";

export interface UploadInstructions {
  method: string;
  url: string;
  headers: Record<string, string>;
  expires_in_seconds: number;
}

export interface CreateJobRequest {
  filename: string;
  content_type: string;
  size_bytes: number;
  preset: string;
}

export interface PartUploadInstruction {
  part_number: number;
  url: string;
}

export interface MultipartUploadInstructions {
  upload_id: string;
  part_size_bytes: number;
  parts: PartUploadInstruction[];
  complete_path: string;
  abort_path: string;
  expires_in_seconds: number;
}

export interface CreateJobResponse {
  job_id: string;
  status: JobStatus;
  preset: string;
  input_bucket: string;
  input_key: string;
  output_key: string;
  upload: UploadInstructions | null;
  multipart_upload: MultipartUploadInstructions | null;
}

export interface JobResponse {
  job_id: string;
  status: JobStatus;
  preset: string;
  attempt_count: number;
  input_key: string;
  output_key: string;
  last_error: string | null;
  lease_owner: string | null;
  lease_expires_at: string | null;
  created_at: string;
  updated_at: string;
}
