/**
 * The signed-in viewer's consent: the identity module's `GET` and `PUT /consent`, beside `POST /playback`, with the
 * viewer's access token. The Pod Decision Service reads it on every break of the viewer's sessions, within 5 seconds
 * of a change:
 *
 * - `opt_out` (sale and sharing): CONTEXTUAL breaks, no identifier or user data for any bidder;
 * - `vppa` (the separate video-privacy consent to share what they watch): FULL;
 * - neither: PSEUDONYMOUS;
 * - no record: SAFE, own-book and house ads only, until the viewer chooses.
 */

export interface ConsentChoice {
  /** Opted out of the sale and sharing of personal information. */
  readonly opt_out: boolean;
  /** Gave the separate video-privacy consent to share what they watch. */
  readonly vppa: boolean;
}

export interface ConsentRecord extends ConsentChoice {
  /** When the viewer chose, epoch milliseconds. */
  readonly updated_at: number;
}

/** The privacy mode a consent gives the viewer's breaks (docs/identity.md#consent). */
export type ConsentMode = "FULL" | "PSEUDONYMOUS" | "CONTEXTUAL" | "SAFE";

/** `…/v1/consent` beside `…/v1/playback`. */
export function consentUrlOf(playbackUrl: string): string {
  return new URL("consent", playbackUrl).href;
}

export function modeOf(record: ConsentChoice | null): ConsentMode {
  if (record === null) {
    return "SAFE";
  }
  return record.opt_out ? "CONTEXTUAL" : record.vppa ? "FULL" : "PSEUDONYMOUS";
}

async function answer(response: Response): Promise<ConsentRecord> {
  const body = (await response.json().catch(() => ({}))) as Partial<ConsentRecord> & { message?: string };
  if (!response.ok || typeof body.opt_out !== "boolean" || typeof body.updated_at !== "number") {
    throw new Error(body.message ?? `The consent API answered ${response.status}`);
  }
  return { opt_out: body.opt_out, vppa: body.vppa === true, updated_at: body.updated_at };
}

/** The viewer's consent, or `null` when they have none. */
export async function readConsent(url: string, accessToken: string, signal?: AbortSignal, fetchImpl: typeof fetch = fetch): Promise<ConsentRecord | null> {
  const response = await fetchImpl(url, { headers: { Authorization: `Bearer ${accessToken}` }, signal });
  return response.status === 404 ? null : answer(response);
}

/** Records the viewer's `choice`; the record as stored. */
export async function writeConsent(url: string, accessToken: string, choice: ConsentChoice, fetchImpl: typeof fetch = fetch): Promise<ConsentRecord> {
  const response = await fetchImpl(url, {
    method: "PUT",
    headers: { Authorization: `Bearer ${accessToken}`, "Content-Type": "application/json" },
    body: JSON.stringify({ opt_out: choice.opt_out, vppa: choice.vppa }),
  });
  return answer(response);
}
