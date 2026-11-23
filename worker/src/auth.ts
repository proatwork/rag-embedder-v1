// The bearer key every request must carry, as mcp_server.KeyGate checks it.
export const AUTHORIZATION = "Authorization";
const BEARER_SCHEME = "bearer"; // compared in any case
export const MIN_KEY_CHARS = 32; // a shorter key could be guessed
const UNAUTHORIZED = 401;
const NOT_CONFIGURED = 500;
export const NO_KEY_REPLY = "This server needs its key: send the header 'Authorization: Bearer <key>'.";
export const NO_KEY_SET_REPLY = "The server has no key set, so it answers nothing.";

/** Whether `key` is usable: long enough, and only characters a header carries as they are. */
export function usableKey(key: string | undefined): key is string {
  return typeof key === "string" && key.trim().length >= MIN_KEY_CHARS && /^[\x21-\x7e]+$/.test(key.trim());
}

async function digest(text: string): Promise<ArrayBuffer> {
  return crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
}

/** Whether `request` carries `key` as `Authorization: Bearer <key>`, compared in constant time. */
export async function carriesKey(request: Request, key: string): Promise<boolean> {
  const [scheme = "", ...rest] = (request.headers.get(AUTHORIZATION) ?? "").trim().split(/\s+/);
  if (scheme.toLowerCase() !== BEARER_SCHEME || rest.length !== 1) return false;
  const [given, expected] = await Promise.all([digest(rest[0] ?? ""), digest(key.trim())]);
  return crypto.subtle.timingSafeEqual(given, expected);
}

export function unauthorized(): Response {
  return new Response(NO_KEY_REPLY, { status: UNAUTHORIZED, headers: { "WWW-Authenticate": "Bearer" } });
}

export function notConfigured(): Response {
  return new Response(NO_KEY_SET_REPLY, { status: NOT_CONFIGURED });
}
