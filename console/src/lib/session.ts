import { createContext, use } from "react";

/** The signed-in operator, independent of how they signed in (Cognito, or mock mode). */
export interface Session {
  readonly user: { readonly name: string; readonly email?: string; readonly groups: readonly string[] };
  /** A currently valid Cognito access token (renewed silently when close to expiry). */
  readonly accessToken: () => Promise<string>;
  readonly signOut: () => void;
}

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = use(SessionContext);
  if (session === null) {
    throw new Error("useSession() outside a SessionContext");
  }
  return session;
}
