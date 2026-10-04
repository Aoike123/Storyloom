import type {
  ApiErrorCode,
  RequestOptions,
  StudioApiError,
  StudioApiResult,
  StudioApiResponse,
} from "./types";

/** ROOT-01 冻结后端端口 3011，永不指向旧 8000 */
const API_BASE: string =
  (typeof process !== "undefined" &&
    process.env?.NEXT_PUBLIC_STUDIO_API_BASE) ||
  "http://127.0.0.1:3011";

const TOKEN_KEY = "sl_studio_token";

// ---------------------------------------------------------------------------
// Storage helpers
// ---------------------------------------------------------------------------

type StorageLike = {
  getItem(k: string): string | null;
  setItem(k: string, v: string): void;
  removeItem(k: string): void;
};

function getToken(
  storage?: StorageLike,
): string | null {
  try {
    const s =
      storage ??
      (typeof window !== "undefined" ? window.localStorage : null);
    if (!s) return null;
    return s.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function setToken(
  t: string | null,
  storage?: StorageLike,
): void {
  try {
    const s =
      storage ??
      (typeof window !== "undefined" ? window.localStorage : null);
    if (!s) return;
    if (t === null) {
      s.removeItem(TOKEN_KEY);
    } else {
      s.setItem(TOKEN_KEY, t);
    }
  } catch {
    // SSR or storage unavailable: no-op
  }
}

// ---------------------------------------------------------------------------
// Command id
// ---------------------------------------------------------------------------

export function newCommandId(): string {
  try {
    if (typeof crypto !== "undefined" && crypto.randomUUID) {
      return crypto.randomUUID();
    }
  } catch {
    // fall through to hex fallback
  }
  // Fallback: 32 random hex chars (16 random bytes)
  const bytes = new Uint8Array(16);
  try {
    if (typeof crypto !== "undefined" && crypto.getRandomValues) {
      crypto.getRandomValues(bytes);
    } else {
      for (let i = 0; i < 16; i++) {
        bytes[i] = Math.floor(Math.random() * 256);
      }
    }
  } catch {
    for (let i = 0; i < 16; i++) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  return Array.from(bytes, (b) =>
    b.toString(16).padStart(2, "0"),
  ).join("");
}

// ---------------------------------------------------------------------------
// Client options
// ---------------------------------------------------------------------------

export interface ClientOptions {
  fetch?: typeof fetch;
  AbortController?: typeof AbortController;
  baseUrl?: string;
  storage?: StorageLike;
}

// ---------------------------------------------------------------------------
// Status → code mapping (for non-contract error bodies)
// ---------------------------------------------------------------------------

function statusToCode(status: number): ApiErrorCode {
  switch (status) {
    case 401:
      return "unauthenticated";
    case 403:
      return "forbidden";
    case 404:
      return "not_found";
    case 409:
      return "revision_conflict";
    case 422:
      return "validation_failed";
    default:
      return "internal_error";
  }
}

// ---------------------------------------------------------------------------
// createClient
// ---------------------------------------------------------------------------

export function createClient(opts?: ClientOptions) {
  const storage = opts?.storage;

  function getTokenFn(): string | null {
    return getToken(storage);
  }

  function setTokenFn(t: string | null): void {
    setToken(t, storage);
  }

  async function request<T>(
    path: string,
    reqOpts?: RequestOptions,
  ): Promise<StudioApiResponse<T>> {
    const baseUrl = opts?.baseUrl ?? API_BASE;
    const url = baseUrl + path;
    const method = reqOpts?.method ?? "GET";
    const timeoutMs = reqOpts?.timeoutMs ?? 30000;
    const fetchFn = opts?.fetch ?? globalThis.fetch;
    const AbortCtor =
      opts?.AbortController ?? globalThis.AbortController;

    // Build headers
    const headers: Record<string, string> = {};
    if (reqOpts?.body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    const token = getTokenFn();
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    // Create abort controller and set up timeout
    const controller = new AbortCtor();
    const timer = setTimeout(() => controller.abort(), timeoutMs);

    // External signal already aborted → return network error immediately
    if (reqOpts?.signal?.aborted) {
      clearTimeout(timer);
      return {
        ok: false,
        code: "network",
        message: "\u8fde\u63a5\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5\u3002",
      };
    }

    // Listen to external signal for abort
    if (reqOpts?.signal) {
      reqOpts.signal.addEventListener("abort", () =>
        controller.abort(),
      );
    }

    try {
      const res = await fetchFn(url, {
        method,
        headers,
        body:
          reqOpts?.body !== undefined
            ? JSON.stringify(reqOpts.body)
            : undefined,
        signal: controller.signal,
      });

      clearTimeout(timer);

      if (!res.ok) {
        // Non-2xx: try to parse the error body
        let parsed: Record<string, any> | null = null;
        try {
          const text = await res.text();
          parsed = JSON.parse(text) as Record<string, any>;
        } catch {
          parsed = null;
        }

        let code: ApiErrorCode;
        let message: string;
        let details: Record<string, unknown> | undefined;

        if (
          parsed !== null &&
          typeof parsed === "object" &&
          parsed.error !== null &&
          typeof parsed.error === "object"
        ) {
          const errObj = parsed.error as Record<string, any>;
          if (typeof errObj.code === "string") {
            code = errObj.code as ApiErrorCode;
          } else {
            code = statusToCode(res.status);
          }
          if (typeof errObj.message === "string") {
            message = errObj.message;
          } else {
            message = `\u8bf7\u6c42\u5931\u8d25\uff08HTTP ${res.status}\uff09\uff0c\u8bf7\u91cd\u8bd5\u3002`;
          }
          if (errObj.details !== undefined) {
            details = errObj.details as Record<string, unknown>;
          }
        } else {
          code = statusToCode(res.status);
          message = `\u8bf7\u6c42\u5931\u8d25\uff08HTTP ${res.status}\uff09\uff0c\u8bf7\u91cd\u8bd5\u3002`;
        }

        const result: StudioApiError = {
          ok: false,
          status: res.status,
          code,
          message,
        };
        if (details !== undefined) {
          result.details = details;
        }
        return result;
      }

      // 2xx: parse success body
      const data = await res.json();
      return { ok: true, data } as StudioApiResult<T>;
    } catch {
      clearTimeout(timer);
      // fetch rejected or aborted
      return {
        ok: false,
        code: "network",
        message: "\u8fde\u63a5\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5\u3002",
      };
    }
  }

  function get<T>(
    path: string,
    reqOpts?: RequestOptions,
  ): Promise<StudioApiResponse<T>> {
    return request<T>(path, { ...reqOpts, method: "GET" });
  }

  function post<T>(
    path: string,
    body?: unknown,
    reqOpts?: RequestOptions,
  ): Promise<StudioApiResponse<T>> {
    const commandId = reqOpts?.commandId ?? newCommandId();
    const mergedBody: Record<string, unknown> = {
      ...(body as Record<string, unknown> ?? {}),
      command_id: commandId,
    };
    return request<T>(path, {
      ...reqOpts,
      method: "POST",
      body: mergedBody,
    });
  }

  return {
    request,
    get,
    post,
    getToken: getTokenFn,
    setToken: setTokenFn,
  };
}

const api = createClient();
export default api;
