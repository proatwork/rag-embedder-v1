import { describe, expect, it } from "vitest";
import { carriesKey, notConfigured, unauthorized, usableKey } from "../src/auth";

const KEY = "k".repeat(31) + "!";

function requestWith(authorization?: string): Request {
  return new Request(
    "https://example.test/mcp",
    authorization === undefined ? {} : { headers: { Authorization: authorization } },
  );
}

describe("carriesKey", () => {
  it("accepts the exact key under the Bearer scheme", async () => {
    expect(await carriesKey(requestWith(`Bearer ${KEY}`), KEY)).toBe(true);
  });

  it.each(["bearer", "BEARER", "bEaReR"])("accepts the scheme spelled %s", async (scheme) => {
    expect(await carriesKey(requestWith(`${scheme} ${KEY}`), KEY)).toBe(true);
  });

  it("rejects a request with no Authorization header", async () => {
    expect(await carriesKey(requestWith(), KEY)).toBe(false);
  });

  it("rejects a wrong key of the same length", async () => {
    expect(await carriesKey(requestWith(`Bearer ${"x".repeat(32)}`), KEY)).toBe(false);
  });

  it("rejects a key that only differs in case", async () => {
    expect(await carriesKey(requestWith(`Bearer ${KEY.toUpperCase()}`), KEY)).toBe(false);
  });

  it("rejects a prefix of the key and the key with extra characters", async () => {
    expect(await carriesKey(requestWith(`Bearer ${KEY.slice(0, -1)}`), KEY)).toBe(false);
    expect(await carriesKey(requestWith(`Bearer ${KEY}x`), KEY)).toBe(false);
  });

  it("rejects the key with no scheme", async () => {
    expect(await carriesKey(requestWith(KEY), KEY)).toBe(false);
  });

  it("rejects the scheme with no key", async () => {
    expect(await carriesKey(requestWith("Bearer"), KEY)).toBe(false);
  });

  it("rejects another scheme carrying the right key", async () => {
    expect(await carriesKey(requestWith(`Basic ${KEY}`), KEY)).toBe(false);
  });

  it("rejects extra tokens after the key", async () => {
    expect(await carriesKey(requestWith(`Bearer ${KEY} extra`), KEY)).toBe(false);
  });

  it("rejects extra tokens before the key", async () => {
    expect(await carriesKey(requestWith(`Bearer extra ${KEY}`), KEY)).toBe(false);
  });

  it("rejects an empty header value", async () => {
    expect(await carriesKey(requestWith(""), KEY)).toBe(false);
  });
});

describe("usableKey", () => {
  it("accepts a key of exactly 32 printable ASCII characters", () => {
    expect(usableKey("a".repeat(32))).toBe(true);
  });

  it("accepts printable punctuation at both ends of the ASCII range", () => {
    expect(usableKey("!" + "a".repeat(30) + "~")).toBe(true);
  });

  it("rejects a key of 31 characters", () => {
    expect(usableKey("a".repeat(31))).toBe(false);
  });

  it("rejects undefined and the empty string", () => {
    expect(usableKey(undefined)).toBe(false);
    expect(usableKey("")).toBe(false);
  });

  it("rejects a long key with a space inside", () => {
    expect(usableKey("a".repeat(20) + " " + "a".repeat(20))).toBe(false);
  });

  it("rejects a long key with a tab inside", () => {
    expect(usableKey("a".repeat(20) + "\t" + "a".repeat(20))).toBe(false);
  });

  it("rejects a long key with a non-ASCII character", () => {
    expect(usableKey("a".repeat(40) + "é")).toBe(false);
  });

  it("rejects a key of spaces only", () => {
    expect(usableKey(" ".repeat(40))).toBe(false);
  });

  it("rejects a key with a control character", () => {
    expect(usableKey("a".repeat(40) + "\u0001")).toBe(false);
  });
});

describe("responses", () => {
  it("unauthorized answers 401 with a Bearer challenge", () => {
    const response = unauthorized();

    expect(response.status).toBe(401);
    expect(response.headers.get("WWW-Authenticate")).toBe("Bearer");
  });

  it("notConfigured answers 500", () => {
    expect(notConfigured().status).toBe(500);
  });
});
