/**
 * Operator identity: Cognito managed login (Authorization Code + PKCE) through react-oidc-context, exposed to the
 * rest of the console as a small `Session` so views never touch OIDC directly (and mock mode can stand in).
 */
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Spinner from "@cloudscape-design/components/spinner";
import { UserManager, type User } from "oidc-client-ts";
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { AuthProvider, useAuth, useAutoSignin } from "react-oidc-context";
import type { AppConfig } from "../lib/config";
import { SessionContext, type Session } from "../lib/session";

/** Resource-server scope required by the Runtime and Gateway JWT authorizers (SPEC §6). */
const SCOPE = "openid email profile fast-channels/agent.invoke";
const RENEW_BEFORE_EXPIRY_S = 60;

function userManager(config: AppConfig): UserManager {
  const home = `${window.location.origin}/`;
  return new UserManager({
    authority: `https://cognito-idp.${config.region}.amazonaws.com/${config.userPoolId}`,
    client_id: config.userPoolClientId,
    redirect_uri: home,
    post_logout_redirect_uri: home,
    response_type: "code",
    scope: SCOPE,
    automaticSilentRenew: true,
    // Cognito's discovery document lists no revocation endpoint; it lives on the managed-login domain.
    metadataSeed: { revocation_endpoint: `${config.cognitoDomain}/oauth2/revoke` },
  });
}

/** Drops `?code=&state=` from the address bar and returns to the view the user started from. */
function onSigninCallback(user: User | undefined): void {
  window.history.replaceState(null, "", `/${typeof user?.state === "string" ? user.state : ""}`);
}

/** Signs the user in (redirecting to managed login when needed) and provides the `Session`. */
export function CognitoSession({ config, children }: { config: AppConfig; children: ReactNode }) {
  const [manager] = useState(() => userManager(config));
  return (
    <AuthProvider userManager={manager} onSigninCallback={onSigninCallback}>
      <SignedIn config={config} manager={manager}>
        {children}
      </SignedIn>
    </AuthProvider>
  );
}

function SignedIn({ config, manager, children }: { config: AppConfig; manager: UserManager; children: ReactNode }) {
  const auth = useAuth();
  const user = auth.user;
  const { isLoading, error } = useAutoSignin({ signinArgs: { state: window.location.hash } });

  // Stable across token renewals, so clients built on it (and their effects) are not recreated every hour.
  const accessToken = useCallback(async () => {
    const current = await manager.getUser();
    const fresh = current && (current.expires_in ?? 0) > RENEW_BEFORE_EXPIRY_S ? current : await manager.signinSilent();
    if (!fresh) {
      throw new Error("Your session has expired; sign in again.");
    }
    return fresh.access_token;
  }, [manager]);

  const signOut = useCallback(() => {
    // Cognito has no OIDC end_session_endpoint: revoke the refresh token, then end the managed-login session.
    const logout = new URL(`${config.cognitoDomain}/logout`);
    logout.searchParams.set("client_id", config.userPoolClientId);
    logout.searchParams.set("logout_uri", `${window.location.origin}/`);
    void manager
      .revokeTokens(["refresh_token"])
      .catch(() => undefined)
      .then(() => manager.removeUser())
      .then(() => {
        window.location.assign(logout);
      });
  }, [config, manager]);

  const session = useMemo<Session | null>(() => {
    if (!user) {
      return null;
    }
    const groups = user.profile["cognito:groups"];
    return {
      user: {
        name: user.profile.name ?? user.profile.email ?? user.profile.sub,
        email: user.profile.email,
        groups: Array.isArray(groups) ? groups.map(String) : [],
      },
      accessToken,
      signOut,
    };
  }, [accessToken, signOut, user]);

  if (error) {
    return (
      <Box padding="xxl">
        <Alert type="error" header="Sign-in failed" action={<Button onClick={() => void auth.signinRedirect()}>Try again</Button>}>
          {error.message}
        </Alert>
      </Box>
    );
  }
  if (isLoading || session === null) {
    return (
      <Box padding="xxl" textAlign="center">
        <Spinner size="large" /> Signing in…
      </Box>
    );
  }
  return <SessionContext value={session}>{children}</SessionContext>;
}
