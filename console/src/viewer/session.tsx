/**
 * Who is watching in this tab: a persona signed in to the viewer pool, a guest persona, or nobody yet. The persona of
 * a signed-in viewer is the one Cognito names (`custom:persona` in the ID token), not the one the picker was
 * clicked for, so the app always shows who the playback API will see.
 */
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { idClaims, loadTokens, renewingAccessToken, storeTokens, type Tokens } from "./auth";
import { useViewerBackend } from "./backend";
import type { Persona } from "./config";
import { ViewerContext, type Viewer } from "./viewer";

const GUEST_KEY = "fast-viewer.guest";

function personaOf(tokens: Tokens, personas: readonly Persona[]): Persona | null {
  try {
    const claims = idClaims(tokens.idToken);
    const id = claims["custom:persona"];
    return personas.find((p) => p.id === id) ?? personas.find((p) => p.email !== null && p.email === claims.email) ?? null;
  } catch {
    return null;
  }
}

export function ViewerSession({ children }: { children: ReactNode }) {
  const backend = useViewerBackend();
  const personas = backend.config.personas;
  const [tokens, setTokens] = useState<Tokens | null>(() => loadTokens());
  const [guest, setGuest] = useState<Persona | null>(() => personas.find((p) => p.id === sessionStorage.getItem(GUEST_KEY)) ?? null);

  const remember = useCallback((value: Tokens | null) => {
    storeTokens(value);
    setTokens(value);
  }, []);

  const signOut = useCallback(() => {
    remember(null);
    sessionStorage.removeItem(GUEST_KEY);
    setGuest(null);
    window.location.hash = "#/personas";
  }, [remember]);

  // Renewed tokens are stored but not put in state: the token function, and the players built on it, stay the same.
  const accessToken = useMemo(
    () =>
      tokens
        ? renewingAccessToken(
            tokens,
            (refreshToken) => backend.refresh(refreshToken),
            (renewed) => {
              storeTokens(renewed);
            },
            signOut,
          )
        : undefined,
    [backend, signOut, tokens],
  );

  const viewer = useMemo<Viewer>(
    () => ({
      persona: tokens ? personaOf(tokens, personas) : guest,
      ...(accessToken ? { accessToken } : {}),
      signIn: async (persona, password) => {
        if (!persona.email) {
          throw new Error(`${persona.name} has no sign-in`);
        }
        const signedIn = await backend.signIn(persona.email, password);
        sessionStorage.removeItem(GUEST_KEY);
        setGuest(null);
        remember(signedIn);
      },
      watchAsGuest: (persona) => {
        remember(null);
        sessionStorage.setItem(GUEST_KEY, persona.id);
        setGuest(persona);
      },
      signOut,
    }),
    [accessToken, backend, guest, personas, remember, signOut, tokens],
  );
  return <ViewerContext value={viewer}>{children}</ViewerContext>;
}
