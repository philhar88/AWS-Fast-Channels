import { ErrorDetails, ErrorTypes, Events, type ErrorData } from "hls.js";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { describeError, isRetryable, retryDelayMs, superviseSession, type Retry, type Session } from "./retry";

const error = (fields: Partial<ErrorData> = {}): ErrorData => ({
  type: ErrorTypes.NETWORK_ERROR,
  details: ErrorDetails.MANIFEST_LOAD_ERROR,
  fatal: true,
  error: new Error("HTTP 504"),
  response: { url: "https://cdn.example/v1/master/h/fast-demo/hls.m3u8", code: 504, text: "Gateway Timeout" },
  ...fields,
});

/** A stand-in hls.js instance: listeners a test can fire, and whether it was destroyed. */
function fakeSession() {
  const listeners = new Map<string, ((event: string, data: unknown) => void)[]>();
  const session = {
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

/** Supervises fake sessions; `sessions` grows with every (re)open. */
function supervised() {
  const sessions: ReturnType<typeof fakeSession>[] = [];
  const retries: Retry[] = [];
  const close = superviseSession(
    () => {
      const session = fakeSession();
      sessions.push(session);
      return session as unknown as Session;
    },
    (retry) => retries.push(retry),
  );
  const latest = () => sessions.at(-1) ?? fakeSession();
  return { sessions, retries, close, latest, delays: () => retries.map((r) => r.delayMs) };
}

describe("retryDelayMs", () => {
  it("doubles from 2 s and caps at 30 s", () => {
    expect([1, 2, 3, 4, 5, 6, 20].map(retryDelayMs)).toEqual([2_000, 4_000, 8_000, 16_000, 30_000, 30_000, 30_000]);
  });
});

describe("isRetryable", () => {
  it.each<[string, Partial<ErrorData>, boolean]>([
    ["a fatal manifest error (504 while the channel starts)", {}, true],
    ["a fatal media playlist 404", { details: ErrorDetails.LEVEL_LOAD_ERROR, response: { url: "", code: 404 } }, true],
    ["a fatal playlist timeout", { details: ErrorDetails.LEVEL_LOAD_TIMEOUT, response: undefined }, true],
    ["an error hls.js still retries itself", { fatal: false }, false],
    ["a fatal media error", { type: ErrorTypes.MEDIA_ERROR, details: ErrorDetails.BUFFER_APPEND_ERROR }, false],
  ])("%s: %s", (_case, fields, expected) => {
    expect(isRetryable(error(fields))).toBe(expected);
  });

  it("describes the failure with its HTTP status when there is one", () => {
    expect(describeError(error())).toBe("manifestLoadError (HTTP 504)");
    expect(describeError(error({ details: ErrorDetails.MANIFEST_LOAD_TIMEOUT, response: undefined }))).toBe("manifestLoadTimeOut");
  });
});

describe("superviseSession", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reopens the session after fatal network errors with exponential backoff", () => {
    const { sessions, retries, latest, delays } = supervised();
    expect(sessions).toHaveLength(1);

    latest().fire(Events.ERROR, error());
    expect(retries).toEqual([{ attempt: 1, delayMs: 2_000, error: error() }]);
    vi.advanceTimersByTime(1_999);
    expect(sessions).toHaveLength(1);
    expect(sessions[0]?.destroyed).toBe(false); // kept until its replacement opens
    vi.advanceTimersByTime(1);
    expect(sessions).toHaveLength(2);
    expect(sessions[0]?.destroyed).toBe(true);

    for (let reload = 2; reload <= 6; reload++) {
      latest().fire(Events.ERROR, error({ details: ErrorDetails.LEVEL_LOAD_ERROR }));
      vi.advanceTimersByTime(retryDelayMs(reload));
    }
    expect(delays()).toEqual([2_000, 4_000, 8_000, 16_000, 30_000, 30_000]);
    expect(sessions).toHaveLength(7);
    expect(sessions.slice(0, -1).every((s) => s.destroyed)).toBe(true);
  });

  it("starts the backoff over once a session buffers media", () => {
    const { latest, delays } = supervised();
    latest().fire(Events.ERROR, error());
    vi.advanceTimersByTime(2_000);
    latest().fire(Events.ERROR, error());
    vi.advanceTimersByTime(4_000);
    latest().fire(Events.FRAG_BUFFERED, {});
    latest().fire(Events.ERROR, error({ details: ErrorDetails.FRAG_LOAD_ERROR }));
    expect(delays()).toEqual([2_000, 4_000, 2_000]);
  });

  it("schedules one reload per failed session and ignores errors hls.js recovers from", () => {
    const { sessions, retries, latest } = supervised();
    latest().fire(Events.ERROR, error({ fatal: false }));
    latest().fire(Events.ERROR, error({ type: ErrorTypes.MEDIA_ERROR, details: ErrorDetails.BUFFER_APPEND_ERROR }));
    expect(retries).toEqual([]);

    latest().fire(Events.ERROR, error());
    latest().fire(Events.ERROR, error({ details: ErrorDetails.LEVEL_LOAD_ERROR }));
    expect(retries).toHaveLength(1);
    vi.runOnlyPendingTimers();
    expect(sessions).toHaveLength(2);
  });

  it("closes the session and cancels a pending reload", () => {
    const { sessions, close, latest } = supervised();
    latest().fire(Events.ERROR, error());
    close();
    expect(sessions[0]?.destroyed).toBe(true);
    vi.runAllTimers();
    expect(sessions).toHaveLength(1);
  });
});
