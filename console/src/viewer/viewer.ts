import { createContext, use } from "react";
import type { Persona } from "./config";

/** Who is watching in this tab (session.tsx). */
export interface Viewer {
  /** `null` until a persona is picked. */
  readonly persona: Persona | null;
  /** A currently valid access token; absent for a guest. */
  readonly accessToken?: () => Promise<string>;
  signIn(persona: Persona, password: string): Promise<void>;
  watchAsGuest(persona: Persona): void;
  signOut(): void;
}

export const ViewerContext = createContext<Viewer | null>(null);

export function useViewer(): Viewer {
  const viewer = use(ViewerContext);
  if (viewer === null) {
    throw new Error("useViewer() outside a ViewerSession");
  }
  return viewer;
}
