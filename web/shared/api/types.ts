export const API_ERROR_CODES = [
  "unauthenticated",
  "forbidden",
  "not_found",
  "validation_failed",
  "range_invalid",
  "range_overlap",
  "revision_conflict",
  "preview_stale",
  "precondition_failed",
  "duplicate_scope",
  "internal_error",
  "network",
] as const;

export type ApiErrorCode = (typeof API_ERROR_CODES)[number];

export interface StudioApiError {
  ok: false;
  status?: number;
  code: ApiErrorCode;
  message: string;
  details?: Record<string, unknown>;
}

export interface StudioApiResult<T> {
  ok: true;
  data: T;
}

export type StudioApiResponse<T> = StudioApiResult<T> | StudioApiError;

export interface StudioListData<T> {
  items: T[];
  next_cursor: string | null;
}

export interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  commandId?: string;
  timeoutMs?: number;
  signal?: AbortSignal;
}
