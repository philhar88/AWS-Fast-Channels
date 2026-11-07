import { describe, expect, it, vi } from "vitest";
import { consentUrlOf, modeOf, readConsent, writeConsent } from "./consent";

const URL_ = "https://api.example/v1/consent";
const json = (status: number, body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status }));

describe("consent", () => {
  it("lives beside the playback API", () => {
    expect(consentUrlOf("https://api.example/v1/playback")).toBe(URL_);
  });

  it("maps a choice to the mode of the viewer's breaks", () => {
    expect(modeOf(null)).toBe("SAFE");
    expect(modeOf({ opt_out: true, vppa: true })).toBe("CONTEXTUAL");
    expect(modeOf({ opt_out: false, vppa: true })).toBe("FULL");
    expect(modeOf({ opt_out: false, vppa: false })).toBe("PSEUDONYMOUS");
  });

  it("reads none as null and a record with the access token", async () => {
    await expect(readConsent(URL_, "tok", undefined, () => json(404, { error: "no_consent" }))).resolves.toBeNull();
    const fetchImpl = vi.fn(() => json(200, { opt_out: false, vppa: true, updated_at: 5 }));
    await expect(readConsent(URL_, "tok", undefined, fetchImpl)).resolves.toEqual({ opt_out: false, vppa: true, updated_at: 5 });
    const [, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.headers).toMatchObject({ Authorization: "Bearer tok" });
  });

  it("puts the choice and raises a refusal", async () => {
    const fetchImpl = vi.fn(() => json(200, { opt_out: true, vppa: false, updated_at: 9 }));
    await expect(writeConsent(URL_, "tok", { opt_out: true, vppa: false }, fetchImpl)).resolves.toMatchObject({ opt_out: true });
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(URL_);
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({ opt_out: true, vppa: false });
    await expect(writeConsent(URL_, "tok", { opt_out: true, vppa: false }, () => json(400, { message: "bad" }))).rejects.toThrow("bad");
  });
});
