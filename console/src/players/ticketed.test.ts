import { ErrorDetails, ErrorTypes, Events, type ErrorData } from "hls.js";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Session } from "./retry";
import { FatalSourceError, superviseTicketedSession, type TicketedRetry } from "./ticketed";

const fatal: ErrorData = {
  type: ErrorTypes.NETWORK_ERROR,
  details: ErrorDetails.MANIFEST_LOAD_ERROR,
  fatal: true,
  error: new Error("HTTP 504"),
  response: { url: "https://cdn.example/v1/master/h/fastm-demo-p-a/hls.m3u8", code: 504, text: "Gateway Timeout" },
};

function fakeSession(url: string) {
  const listeners = new Map<string, ((event: string, data: unknown) => void)[]>();
  const session = {
    url,
    destroyed: false,
    on: (name: string, fn: (event: string, data: unknown) => void) => listeners.set(name, [...(listeners.get(name) ?? []), fn]),
    destroy: () => {
      session.destroyed = true;
      listeners.clear();
    },
    fire: (name: string, data?: unknown) => {
      for (const fn of listeners.get(name) ?? []) {
        fn(name, data);
      }
    },
  };
  return session;
}

/** A source that mints `t1`, `t2`, … and the sessions opened on them. */
function supervised(mint: (n: number, signal: AbortSignal) => Promise<string> = (n) => Promise.resolve(`t${n}`)) {
  const sessions: ReturnType<typeof fakeSession>[] = [];
  const retries: TicketedRetry[] = [];
  const fatals: string[] = [];
  const signals: AbortSignal[] = [];
  let mints = 0;
  const close = superviseTicketedSession(
    (signal) => {
      mints += 1;
      signals.push(signal);
      return mint(mints, signal);
    },
    (url) => {
      const session = fakeSession(url);
      sessions.push(session);
      return session as unknown as Session;
    },
    { onRetry: (retry) => retries.push(retry), onFatal: (reason) => fatals.push(reason) },
  );
  return { sessions, retries, fatals, signals, close, mints: () => mints, latest: () => sessions.at(-1) ?? fakeSession("") };
}

describe("superviseTicketedSession", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("mints a new ticket for every reopen", async () => {
    const { sessions, retries, latest, mints } = supervised();
    await vi.advanceTimersByTimeAsync(0);
    expect(sessions.map((s) => s.url)).toEqual(["t1"]);

    latest().fire(Events.ERROR, fatal);
    expect(retries).toEqual([{ attempt: 1, delayMs: 2_000, reason: "manifestLoadError (HTTP 504)" }]);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(sessions.map((s) => s.url)).toEqual(["t1", "t2"]);
    expect(sessions[0]?.destroyed).toBe(true);
    expect(mints()).toBe(2);
  });

  it("retries a failed mint with the same backoff, and resets it once media buffers", async () => {
    const { sessions, retries, latest } = supervised((n) => (n === 1 ? Promise.reject(new Error("HTTP 503")) : Promise.resolve(`t${n}`)));
    await vi.advanceTimersByTimeAsync(0);
    expect(retries).toEqual([{ attempt: 1, delayMs: 2_000, reason: "HTTP 503" }]);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(sessions.map((s) => s.url)).toEqual(["t2"]);

    latest().fire(Events.FRAG_BUFFERED, {});
    latest().fire(Events.ERROR, fatal);
    expect(retries.at(-1)?.delayMs).toBe(2_000);
  });

  it("stops on a fatal source error", async () => {
    const { sessions, retries, fatals } = supervised(() => Promise.reject(new FatalSourceError("Sign in again")));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fatals).toEqual(["Sign in again"]);
    expect(retries).toEqual([]);
    expect(sessions).toEqual([]);
  });

  it("ignores errors hls.js recovers from", async () => {
    const { retries, latest } = supervised();
    await vi.advanceTimersByTimeAsync(0);
    latest().fire(Events.ERROR, { ...fatal, fatal: false });
    latest().fire(Events.ERROR, { ...fatal, type: ErrorTypes.MEDIA_ERROR, details: ErrorDetails.BUFFER_APPEND_ERROR });
    expect(retries).toEqual([]);
  });

  it("aborts a mint in flight on close and never opens its session", async () => {
    let resolve: (url: string) => void = () => undefined;
    const { sessions, signals, close } = supervised(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    close();
    expect(signals[0]?.aborted).toBe(true);
    resolve("t1");
    await vi.advanceTimersByTimeAsync(0);
    expect(sessions).toEqual([]);
  });

  it("closes the session and cancels a pending reopen", async () => {
    const { sessions, close, latest, mints } = supervised();
    await vi.advanceTimersByTimeAsync(0);
    latest().fire(Events.ERROR, fatal);
    close();
    expect(sessions[0]?.destroyed).toBe(true);
    await vi.runAllTimersAsync();
    expect(mints()).toBe(1);
  });
});
