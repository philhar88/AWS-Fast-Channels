const pad = (n: number) => String(n).padStart(2, "0");

/** `596000` → `9:56`, `3723000` → `1:02:03`. */
export function formatDuration(ms: number): string {
  const total = Math.round(ms / 1000);
  const [h, m, s] = [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60];
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

const clock = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const clockSeconds = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export const formatClock = (time: Date | number | string): string => clock.format(new Date(time));
export const formatClockSeconds = (time: Date | number | string): string => clockSeconds.format(new Date(time));
export const formatDateTime = (time: Date | number | string): string => dateTime.format(new Date(time));

export const formatUsd = (value: number): string =>
  value.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: value < 1 ? 4 : 2 });
