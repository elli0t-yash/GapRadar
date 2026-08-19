// The ONLY module that knows the backend's address or speaks HTTP.
// Components never call fetch() directly.

const BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export const API_V1 = `${BASE_URL}/api/v1`;

/** The request reached the server and it answered with a non-2xx. */
export class ApiError extends Error {
  readonly status: number;
  readonly body?: unknown;

  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }

  get isNotFound(): boolean {
    return this.status === 404;
  }
}

/**
 * The request never reached the server: offline, DNS, or CORS.
 *
 * A CORS rejection is indistinguishable from "backend down" in the browser --
 * both surface as an opaque TypeError from fetch -- so the copy for this
 * error has to cover both causes.
 */
export class NetworkError extends Error {
  readonly reason?: unknown;

  constructor(message: string, reason?: unknown) {
    super(message);
    this.name = "NetworkError";
    this.reason = reason;
  }
}

function messageFrom(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    // 404 / 500 -> string. 422 -> array of validation items.
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail
        .map((item) =>
          typeof item === "object" && item !== null && "msg" in item
            ? String((item as { msg: unknown }).msg)
            : String(item),
        )
        .join("; ");
    }
  }
  return `Request failed with status ${status}`;
}

export async function apiGet<T>(
  path: string,
  options: {
    params?: Record<string, string | number | undefined>;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  const url = new URL(`${API_V1}${path}`);
  for (const [key, value] of Object.entries(options.params ?? {})) {
    if (value !== undefined) url.searchParams.set(key, String(value));
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: { Accept: "application/json" },
      // No credentials: the API has no auth, and sending them interacts
      // badly with CORS.
      signal: options.signal,
    });
  } catch (cause) {
    // An aborted request is a cancellation, not a failure -- let callers
    // tell the two apart.
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw new NetworkError("Could not reach the GapRadar API", cause);
  }

  // Parsed before the ok check so `detail` is available on an error body.
  const body: unknown = await response.json().catch(() => undefined);
  if (!response.ok) {
    throw new ApiError(response.status, messageFrom(body, response.status), body);
  }
  return body as T;
}

/** True for a cancellation the app itself caused. Never an error state. */
export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
