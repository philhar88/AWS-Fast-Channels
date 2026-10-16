/** Per-codec HLS outputs (SPEC §3 profiles) and whether this browser can decode them through MSE. */
const PROBES: Readonly<Record<string, string>> = {
  "hls-avc": 'video/mp4; codecs="avc1.640028"',
  "hls-hevc": 'video/mp4; codecs="hvc1.1.6.L120.B0"',
  "hls-av1": 'video/mp4; codecs="av01.0.08M.08"',
};

export const PROFILE_LABELS: Readonly<Record<string, string>> = {
  hls: "All codecs",
  "hls-avc": "AVC",
  "hls-hevc": "HEVC",
  "hls-av1": "AV1",
  "hls-sd": "SD",
  "hls-hd": "HD",
  "hls-uhd": "UHD",
};

/** False only when a per-codec profile is known to be undecodable here (the combined `hls` lets the player pick). */
export function canPlayProfile(profile: string): boolean {
  const probe = PROBES[profile];
  return probe === undefined || (typeof MediaSource !== "undefined" && MediaSource.isTypeSupported(probe));
}
