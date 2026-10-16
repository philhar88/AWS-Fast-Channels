import { describe, expect, it } from "vitest";
import { freshAccessToken, RENEW_BEFORE_EXPIRY_S, type TokenStore } from "./tokens";

/** A store whose current token has `expiresIn` seconds left; renewals take a tick and are counted. */
function store(expiresIn: number | null, renewed: { access_token: string } | null = { access_token: "renewed" }) {
  const calls = { renewals: 0 };
  const tokens: TokenStore = {
    getUser: () => Promise.resolve(expiresIn === null ? null : { access_token: "current", expires_in: expiresIn }),
    signinSilent: async () => {
      calls.renewals++;
      await Promise.resolve();
      return renewed;
    },
  };
  return { tokens, calls };
}

describe("freshAccessToken", () => {
  it("renews a token that would expire during an agent run (10 minutes)", async () => {
    expect(RENEW_BEFORE_EXPIRY_S).toBe(600);
    await expect(freshAccessToken(store(601).tokens)()).resolves.toBe("current");
    await expect(freshAccessToken(store(300).tokens)()).resolves.toBe("renewed");
  });

  it("shares one renewal between concurrent callers and renews again later", async () => {
    const { tokens, calls } = store(30);
    const accessToken = freshAccessToken(tokens);

    await expect(Promise.all([accessToken(), accessToken(), accessToken()])).resolves.toEqual(["renewed", "renewed", "renewed"]);
    await accessToken();

    expect(calls.renewals).toBe(2);
  });

  it("asks to sign in again when there is no session to renew", async () => {
    await expect(freshAccessToken(store(null, null).tokens)()).rejects.toThrow("sign in again");
  });
});
