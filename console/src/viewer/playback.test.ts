import { describe, expect, it, vi } from "vitest";
import { FatalSourceError } from "../players/ticketed";
import type { ViewerChannel } from "./config";
import { MintError, mintTicket, openChannel, sessionIdOf } from "./playback";

const API = "https://api.example/v1/";
const MINT = `${API}playback`;
const PLAN = "https://cdn.example/plan/playback";
const TICKETED = "https://cdn.example/v1/master/h/fastm-demo-p-a/hls.m3u8?ads.t=AAAAAAAAAAAAAAAAAAAAAA";
const PLANNED = "https://cdn.example/v1/master/h/fastm-demo-p-a/hls.m3u8";
const V3 = "https://cdn.example/v1/master/h/fastm-demo/hls.m3u8";
const DEMO: ViewerChannel = { name: "fastm-demo", monetized: true, playback: { hls: V3 } };

const json = (status: number, body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status }));

const urlOf = (input: string | URL | Request) => (typeof input === "string" ? input : input instanceof URL ? input.href : input.url);

/** Routes the playback API and the plan; records every request. */
function backend(mint: () => Promise<Response>, plan: () => Promise<Response> = () => json(200, { playback: { hls: PLANNED } })) {
  return vi.fn((input: string | URL | Request) => (urlOf(input).startsWith(API) ? mint() : plan()));
}

describe("mintTicket", () => {
  it("posts the channel with the access token", async () => {
    const fetchImpl = vi.fn(() => json(200, { url: TICKETED, cid: "01J", expires_in: 120 }));
    await expect(mintTicket(MINT, "tok", "fastm-demo", undefined, fetchImpl)).resolves.toEqual({
      url: TICKETED,
      cid: "01J",
    });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.example/v1/playback");
    expect(init.method).toBe("POST");
    expect(init.headers).toMatchObject({ Authorization: "Bearer tok" });
    expect(JSON.parse(init.body as string)).toEqual({ channel: "fastm-demo" });
  });

  it("raises the API's error code", async () => {
    await expect(mintTicket(MINT, "t", "x", undefined, () => json(503, { error: "not_released", message: "No stable sibling" }))).rejects.toEqual(
      new MintError("No stable sibling", "not_released", 503),
    );
    await expect(mintTicket(MINT, "t", "x", undefined, () => json(401, { message: "Unauthorized" }))).rejects.toMatchObject({
      code: "unauthorized",
      status: 401,
    });
  });
});

describe("openChannel", () => {
  const token = () => Promise.resolve("tok");

  it("mints a ticket for a signed-in viewer of a monetized channel", async () => {
    const fetchImpl = backend(() => json(200, { url: TICKETED, cid: "01J" }));
    await expect(openChannel(DEMO, { playbackUrl: MINT, planBaseUrl: PLAN, accessToken: token, fetchImpl })).resolves.toEqual({
      url: TICKETED,
      cid: "01J",
    });
  });

  it("mints a guest ticket with no token, carrying Global Privacy Control", async () => {
    const fetchImpl = backend(() => json(200, { url: TICKETED, cid: "01G" }));
    await expect(openChannel(DEMO, { playbackUrl: MINT, planBaseUrl: PLAN, gpc: true, fetchImpl })).resolves.toEqual({
      url: TICKETED,
      cid: "01G",
    });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(`${MINT}/guest`);
    expect(init.headers).not.toHaveProperty("Authorization");
    expect(JSON.parse(init.body as string)).toEqual({ channel: "fastm-demo", gpc: true });
  });

  it("plays a guest on the playback plan's anonymous URL when the guest mint fails", async () => {
    const blocked = backend(() => Promise.resolve(new Response("Forbidden", { status: 403 })));
    await expect(openChannel(DEMO, { playbackUrl: MINT, planBaseUrl: PLAN, gpc: false, fetchImpl: blocked })).resolves.toEqual({
      url: PLANNED,
      anonymous: "guest; no ticket: unauthorized",
    });
    const [, init] = blocked.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ channel: "fastm-demo" });
    expect(urlOf(blocked.mock.calls[1]?.[0] ?? "")).toBe(`${PLAN}/fastm-demo.json`);
  });

  it("falls back to anonymous playback when the mint fails", async () => {
    const down = backend(() => Promise.reject(new TypeError("Failed to fetch")));
    await expect(openChannel(DEMO, { playbackUrl: MINT, planBaseUrl: PLAN, accessToken: token, fetchImpl: down })).resolves.toEqual({
      url: PLANNED,
      anonymous: "no ticket: the playback API is unreachable",
    });
    const unreleased = backend(
      () => json(503, { error: "not_released" }),
      () => json(404, {}),
    );
    await expect(
      openChannel(DEMO, { playbackUrl: MINT, planBaseUrl: PLAN, accessToken: token, fetchImpl: unreleased }),
    ).resolves.toEqual({ url: V3, anonymous: "no ticket: not_released; no playback plan" });
  });

  it("stops on an authorization failure instead of hiding it", async () => {
    const fetchImpl = backend(() => json(403, { message: "Forbidden" }));
    await expect(openChannel(DEMO, { playbackUrl: MINT, planBaseUrl: PLAN, accessToken: token, fetchImpl })).rejects.toBeInstanceOf(
      FatalSourceError,
    );
  });

  it("plays an unmonetized channel on its v3 URL without asking the API", async () => {
    const fetchImpl = vi.fn();
    const channel = { ...DEMO, name: "fastm-anim", monetized: false };
    await expect(openChannel(channel, { playbackUrl: MINT, planBaseUrl: PLAN, accessToken: token, fetchImpl })).resolves.toEqual({
      url: V3,
      anonymous: "not monetized",
    });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});

describe("sessionIdOf", () => {
  it("reads MediaTailor's session id from a variant playlist URL", () => {
    // As MediaTailor writes it for a Channel Assembly source (captured on a stage, 2026-11-05).
    const variant = "https://cdn.example/v1/manifest/1bab6b42/fastcij2-demo/82e94114-f667-40bc-afc9-96136b2e07c9/0.m3u8";
    expect(sessionIdOf(variant)).toBe("82e94114-f667-40bc-afc9-96136b2e07c9");
    expect(sessionIdOf("https://cdn.example/v1/manifest/1bab/0f8e2b5c-3d0a-4a5e-9c1b-2f6d7e8a9b0c/0.m3u8")).toBe(
      "0f8e2b5c-3d0a-4a5e-9c1b-2f6d7e8a9b0c",
    );
    expect(sessionIdOf("https://cdn.example/v1/master/h/fastm-demo/hls.m3u8")).toBeUndefined();
  });
});
