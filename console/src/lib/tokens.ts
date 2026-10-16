/**
 * Seconds of validity an access token must have left to be used. The agent forwards the operator's token to the
 * Gateway, which checks `exp` on every tool call of the run, and plan or publish runs take minutes. Renewing at
 * 10 minutes covers a whole run. It requires tokens that live longer than this (Cognito issues 60-minute ones),
 * otherwise every call would renew.
 */
export const RENEW_BEFORE_EXPIRY_S = 600;

interface Token {
  readonly access_token: string;
  /** Seconds until expiry. */
  readonly expires_in?: number;
}

/** What `oidc-client-ts`' `UserManager` offers for this. */
export interface TokenStore {
  getUser(): Promise<Token | null>;
  signinSilent(): Promise<Token | null>;
}

/**
 * The session's `accessToken()`: the stored token while at least `marginS` seconds remain, otherwise a renewed one.
 * Concurrent callers share one renewal, so a burst of requests spends the rotating refresh token once.
 */
export function freshAccessToken(store: TokenStore, marginS = RENEW_BEFORE_EXPIRY_S): () => Promise<string> {
  let renewal: Promise<Token | null> | null = null;
  const renew = () =>
    (renewal ??= store.signinSilent().finally(() => {
      renewal = null;
    }));
  return async () => {
    const current = await store.getUser();
    const fresh = current && (current.expires_in ?? 0) > marginS ? current : await renew();
    if (!fresh) {
      throw new Error("Your session has expired; sign in again.");
    }
    return fresh.access_token;
  };
}
