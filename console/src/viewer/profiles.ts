/**
 * Kids profiles: a kids persona has no sign-in of its own. It signs in with its account's (`account`, a persona of
 * the viewer pool), and every play then asks `POST /playback` for the kids profile (`profile: "kids"`), which answers
 * the channel's kids twin: KIDS breaks, no ticket and no viewer id.
 */
import type { Persona } from "./config";

/** The sign-in a persona uses: its own, or for a kids profile its account's; `null` for a guest. */
export function signInEmail(persona: Persona, personas: readonly Persona[]): string | null {
  return persona.email ?? personas.find((p) => p.id === persona.account)?.email ?? null;
}

/** The kids profile `id` names, if it is one of `account`'s. */
export function kidsProfileOf(id: string | null, account: Persona | null, personas: readonly Persona[]): Persona | undefined {
  const profile = personas.find((p) => p.id === id && p.signIn === "kids_profile");
  return profile && account && profile.account === account.id ? profile : undefined;
}
