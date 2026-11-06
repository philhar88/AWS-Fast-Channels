import { describe, expect, it, vi } from "vitest";
import {
  idClaims,
  jwtClaims,
  loadTokens,
  refreshTokens,
  renewingAccessToken,
  signIn,
  SignInError,
  storeTokens,
  type Tokens,
} from "./auth";

const base64url = (text: string) =>
  btoa(String.fromCharCode(...new TextEncoder().encode(text)))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
const jwt = (claims: Record<string, unknown>) => ["e30", base64url(JSON.stringify(claims)), "sig"].join(".");

const json = (status: number, body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status }));

function memoryStorage(): Storage {
  const items = new Map<string, string>();
  return {
    get length() {
      return items.size;
    },
    clear: () => {
      items.clear();
    },
    getItem: (key) => items.get(key) ?? null,
    key: (index) => [...items.keys()][index] ?? null,
    removeItem: (key) => items.delete(key),
    setItem: (key, value) => items.set(key, value),
  };
}

describe("signIn", () => {
  it("calls InitiateAuth with USER_PASSWORD_AUTH and keeps the tokens", async () => {
    const fetchImpl = vi.fn(() =>
      json(200, { AuthenticationResult: { AccessToken: "a", IdToken: "i", RefreshToken: "r", ExpiresIn: 3600 } }),
    );
    const before = Date.now();
    const tokens = await signIn("us-east-1", "client", "dana@personas.example.com", "pw", fetchImpl);

    expect(tokens).toMatchObject({ accessToken: "a", idToken: "i", refreshToken: "r" });
    expect(tokens.expiresAt).toBeGreaterThanOrEqual(before + 3_600_000);
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://cognito-idp.us-east-1.amazonaws.com/");
    expect(init.headers).toMatchObject({ "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth" });
    expect(JSON.parse(init.body as string)).toEqual({
      AuthFlow: "USER_PASSWORD_AUTH",
      ClientId: "client",
      AuthParameters: { USERNAME: "dana@personas.example.com", PASSWORD: "pw" },
    });
  });

  it("reports Cognito's error and refuses challenges", async () => {
    const refused = signIn("us-east-1", "c", "u", "bad", () =>
      json(400, { __type: "NotAuthorizedException", message: "Incorrect username or password." }),
    );
    await expect(refused).rejects.toEqual(new SignInError("Incorrect username or password.", "NotAuthorizedException"));
    const challenged = signIn("us-east-1", "c", "u", "pw", () => json(200, { ChallengeName: "NEW_PASSWORD_REQUIRED" }));
    await expect(challenged).rejects.toMatchObject({ code: "ChallengeRequired" });
  });

  it("renews with the refresh token and keeps it", async () => {
    const fetchImpl = vi.fn(() => json(200, { AuthenticationResult: { AccessToken: "a2", IdToken: "i2", ExpiresIn: 3600 } }));
    const tokens = await refreshTokens("us-east-1", "client", "r", fetchImpl);
    expect(tokens).toMatchObject({ accessToken: "a2", refreshToken: "r" });
    const [, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toMatchObject({ AuthFlow: "REFRESH_TOKEN_AUTH", AuthParameters: { REFRESH_TOKEN: "r" } });
  });
});

describe("claims", () => {
  it("reads the persona from the ID token", () => {
    const token = jwt({ sub: "s-1", email: "dana@personas.example.com", "custom:persona": "dana", note: "é" });
    expect(idClaims(token)).toMatchObject({ sub: "s-1", "custom:persona": "dana" });
    expect(jwtClaims(token).note).toBe("é");
    expect(() => idClaims(jwt({ email: "x" }))).toThrow("no sub");
    expect(() => jwtClaims("nope")).toThrow("not a JWT");
  });
});

describe("token storage", () => {
  it("round-trips and clears, and drops a corrupt entry", () => {
    const storage = memoryStorage();
    const tokens: Tokens = { accessToken: "a", idToken: "i", expiresAt: 1 };
    storeTokens(tokens, storage);
    expect(loadTokens(storage)).toEqual(tokens);
    storeTokens(null, storage);
    expect(loadTokens(storage)).toBeNull();
    storage.setItem("fast-viewer.tokens", "{");
    expect(loadTokens(storage)).toBeNull();
    expect(storage.length).toBe(0);
  });
});

describe("renewingAccessToken", () => {
  const valid: Tokens = { accessToken: "a1", idToken: "i1", refreshToken: "r", expiresAt: Date.now() + 3_600_000 };
  const expiring: Tokens = { ...valid, expiresAt: Date.now() + 30_000 };

  it("returns the current token while it is valid, and renews once for concurrent callers", async () => {
    const refresh = vi.fn(() => Promise.resolve({ ...valid, accessToken: "a2" }));
    const renewed = vi.fn();
    await expect(renewingAccessToken(valid, refresh, renewed, vi.fn())()).resolves.toBe("a1");
    expect(refresh).not.toHaveBeenCalled();

    const token = renewingAccessToken(expiring, refresh, renewed, vi.fn());
    await expect(Promise.all([token(), token()])).resolves.toEqual(["a2", "a2"]);
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(refresh).toHaveBeenCalledWith("r");
    await expect(token()).resolves.toBe("a2");
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("signs out when the session cannot be renewed", async () => {
    const failed = vi.fn();
    await expect(renewingAccessToken({ ...expiring, refreshToken: undefined }, vi.fn(), vi.fn(), failed)()).rejects.toThrow("sign in again");
    const refused = renewingAccessToken(expiring, () => Promise.reject(new SignInError("Refresh Token has expired", "NotAuthorizedException")), vi.fn(), failed);
    await expect(refused()).rejects.toThrow("Refresh Token has expired");
    expect(failed).toHaveBeenCalledTimes(2);
  });
});
