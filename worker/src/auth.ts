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

/** What a refused Authorization header looked like, for the log: its shape only, never a character of the key. */
export interface RefusedKeyShape {
  header: boolean; // whether there was an Authorization header at all
  bearer: boolean; // whether its first word is the Bearer scheme
  words: number; // words in it; the scheme and the key make 2
  keyChars: number; // characters of what came after the scheme
  expectedChars: number; // characters of the key the server holds
  quoted: boolean; // whether that part starts or ends with a quote
  unusual: boolean; // whether it has characters a key never has: outside printable ASCII
}

const QUOTES = /^["']|["']$/; // curly ones arrive as other bytes, which `unusual` catches
const KEY_CHARS = /^[\x21-\x7e]*$/;

export function refusedKeyShape(request: Request, key: string): RefusedKeyShape {
  const value = request.headers.get(AUTHORIZATION);
  const [scheme = "", ...rest] = (value ?? "").trim().split(/\s+/);
  const given = rest.join(" ");
  return {
    header: value !== null,
    bearer: scheme.toLowerCase() === BEARER_SCHEME,
    words: value?.trim() ? rest.length + 1 : 0,
    keyChars: given.length,
    expectedChars: key.trim().length,
    quoted: QUOTES.test(given),
    unusual: !KEY_CHARS.test(given.replace(/ /g, "")),
  };
}

export function unauthorized(): Response {
  return new Response(NO_KEY_REPLY, { status: UNAUTHORIZED, headers: { "WWW-Authenticate": "Bearer" } });
}

export function notConfigured(): Response {
  return new Response(NO_KEY_SET_REPLY, { status: NOT_CONFIGURED });
}
