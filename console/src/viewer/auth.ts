/**
 * Viewer sign-in against the viewer pool's public client: Cognito `InitiateAuth` with `USER_PASSWORD_AUTH`, and
 * `REFRESH_TOKEN_AUTH` to renew. The browser calls the Cognito user-pools API directly
 * (`https://cognito-idp.<region>.amazonaws.com/`), which answers CORS; there is no hosted page and no redirect.
 *
 * The pool's pre-token-generation trigger adds the scope `fast/playback` to the access token, which is the only
 * token the playback API accepts (it refuses ID tokens, which carry the email address). The ID token is read here,
 * for the persona id (`custom:persona`), and never leaves the page.
 *
 * Tokens are kept in `sessionStorage`, one set per tab: two tabs can hold two personas side by side.
 */

export interface Tokens {
  readonly accessToken: string;
  readonly idToken: string;
  readonly refreshToken?: string;
  /** Epoch milliseconds at which the access token expires. */
  readonly expiresAt: number;
}

/** Claims the app reads from the ID token. */
export interface IdClaims {
  readonly sub: string;
  readonly email?: string;
  readonly "custom:persona"?: string;
}

export class SignInError extends Error {
  override readonly name = "SignInError";
  constructor(
    message: string,
    /** Cognito's error type, e.g. `NotAuthorizedException`, or `ChallengeRequired`. */
    readonly code: string,
  ) {
    super(message);
  }
}

interface AuthenticationResult {
  readonly AccessToken: string;
  readonly IdToken: string;
  readonly RefreshToken?: string;
  readonly ExpiresIn: number;
}

async function initiateAuth(
  region: string,
  body: Record<string, unknown>,
  fetchImpl: typeof fetch,
): Promise<AuthenticationResult> {
  const response = await fetchImpl(`https://cognito-idp.${region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-amz-json-1.1",
      "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
    },
    body: JSON.stringify(body),
  });
  const payload = (await response.json().catch(() => ({}))) as {
    AuthenticationResult?: AuthenticationResult;
    ChallengeName?: string;
    __type?: string;
    message?: string;
  };
  if (!response.ok) {
    const code = (payload.__type ?? `HTTP ${response.status}`).split("#").at(-1) ?? "Error";
    throw new SignInError(payload.message ?? `Sign-in failed (${code})`, code);
  }
  if (payload.ChallengeName) {
    throw new SignInError(`Sign-in needs the ${payload.ChallengeName} challenge, which this demo does not handle`, "ChallengeRequired");
  }
  if (!payload.AuthenticationResult) {
    throw new SignInError("Cognito returned no tokens", "NoTokens");
  }
  return payload.AuthenticationResult;
}

const tokens = (result: AuthenticationResult, now: number, refreshToken?: string): Tokens => ({
  accessToken: result.AccessToken,
  idToken: result.IdToken,
  refreshToken: result.RefreshToken ?? refreshToken,
  expiresAt: now + result.ExpiresIn * 1000,
});

/** Signs `username` in with its password. */
export async function signIn(
  region: string,
  clientId: string,
  username: string,
  password: string,
  fetchImpl: typeof fetch = fetch,
): Promise<Tokens> {
  const result = await initiateAuth(
    region,
    { AuthFlow: "USER_PASSWORD_AUTH", ClientId: clientId, AuthParameters: { USERNAME: username, PASSWORD: password } },
    fetchImpl,
  );
  return tokens(result, Date.now());
}

/** New access and ID tokens for `refreshToken` (Cognito returns no new refresh token for this flow). */
export async function refreshTokens(
  region: string,
  clientId: string,
  refreshToken: string,
  fetchImpl: typeof fetch = fetch,
): Promise<Tokens> {
  const result = await initiateAuth(
    region,
    { AuthFlow: "REFRESH_TOKEN_AUTH", ClientId: clientId, AuthParameters: { REFRESH_TOKEN: refreshToken } },
    fetchImpl,
  );
  return tokens(result, Date.now(), refreshToken);
}

/** The payload of a JWT, unverified: the app only reads its own ID token's claims for display. */
export function jwtClaims(token: string): Record<string, unknown> {
  const payload = token.split(".")[1];
  if (!payload) {
    throw new Error("not a JWT");
  }
  const base64 = payload.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(payload.length / 4) * 4, "=");
  const bytes = Uint8Array.from(atob(base64), (c) => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes)) as Record<string, unknown>;
}

/** The ID token's claims the app reads. */
export function idClaims(idToken: string): IdClaims {
  const claims = jwtClaims(idToken);
  if (typeof claims.sub !== "string") {
    throw new Error("the ID token has no sub");
  }
  return claims as unknown as IdClaims;
}

/** Validity an access token must have left to be sent: a mint takes well under a second. */
export const RENEW_BEFORE_EXPIRY_MS = 60_000;

/**
 * The session's `accessToken()`: the current token while it has {@link RENEW_BEFORE_EXPIRY_MS} left, otherwise one
 * renewed with the refresh token. Concurrent callers share one renewal. `renewed` hears of each new set of tokens;
 * `failed` of a session that cannot be renewed (no refresh token, or Cognito refused it).
 */
export function renewingAccessToken(
  initial: Tokens,
  refresh: (refreshToken: string) => Promise<Tokens>,
  renewed: (tokens: Tokens) => void,
  failed: () => void,
): () => Promise<string> {
  const state = { current: initial, renewal: null as Promise<Tokens> | null };
  return async () => {
    if (state.current.expiresAt - Date.now() > RENEW_BEFORE_EXPIRY_MS) {
      return state.current.accessToken;
    }
    const refreshToken = state.current.refreshToken;
    if (!refreshToken) {
      failed();
      throw new Error("Your session has expired; sign in again.");
    }
    state.renewal ??= refresh(refreshToken).finally(() => {
      state.renewal = null;
    });
    try {
      state.current = await state.renewal;
    } catch (error) {
      failed();
      throw error;
    }
    renewed(state.current);
    return state.current.accessToken;
  };
}

const STORAGE_KEY = "fast-viewer.tokens";

/** The tab's stored tokens, or `null`. */
export function loadTokens(storage: Storage = sessionStorage): Tokens | null {
  const raw = storage.getItem(STORAGE_KEY);
  if (!raw) {
    return null;
  }
  try {
    return JSON.parse(raw) as Tokens;
  } catch {
    storage.removeItem(STORAGE_KEY);
    return null;
  }
}

export function storeTokens(value: Tokens | null, storage: Storage = sessionStorage): void {
  if (value === null) {
    storage.removeItem(STORAGE_KEY);
  } else {
    storage.setItem(STORAGE_KEY, JSON.stringify(value));
  }
}
