// Talks to `agentctl gui`. Every call carries the session token, which the
// server printed into the URL fragment when it opened this page.

const TOKEN_KEY = "agentos-token";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Move the token out of the address bar into this tab's session storage. */
export function captureToken(): string | null {
  const match = window.location.hash.match(/token=([^&]+)/);
  if (match) {
    try {
      sessionStorage.setItem(TOKEN_KEY, match[1]);
    } catch {
      /* private mode: keep it in memory for this page only */
    }
    history.replaceState(null, "", window.location.pathname + window.location.search);
    memoryToken = match[1];
    return match[1];
  }
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? memoryToken;
  } catch {
    return memoryToken;
  }
}

let memoryToken: string | null = null;

async function call<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
  const token = captureToken();
  const response = await fetch(path, {
    method,
    headers: {
      Authorization: `Bearer ${token ?? ""}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new ApiError(response.status, payload.error ?? response.statusText);
  }
  return payload as T;
}

export const api = {
  get: <T,>(path: string) => call<T>("GET", path),
  post: <T,>(path: string, body?: unknown) => call<T>("POST", path, body ?? {}),
};
