// Number and time formats (docs/design/installer-ui.md section 3.4): sizes in GB with one decimal, speeds in MB/s with
// one decimal, time left as "10 min left", clock times as HH:MM on the 24 hour clock.

const GB = 1e9;
const MB = 1e6;

/** 9_000_000_000 -> "9.0". */
export const gbNum = (bytes: number): string => (bytes / GB).toFixed(1);
/** 9_000_000_000 -> "9.0 GB". */
export const gb = (bytes: number): string => `${gbNum(bytes)} GB`;
/** Disk space already in GB: 43 -> "43 GB". */
export const gbFree = (n: number): string => `${Math.round(n)} GB`;
/** 11_400_000 -> "11.4 MB/s". */
export const mbps = (bytesPerSecond: number): string => `${(bytesPerSecond / MB).toFixed(1)} MB/s`;

/** Seconds -> "10 min left", "1 h 05 min left". Rounds up to the whole minute. */
export function minutesLeft(seconds: number): string {
  const m = Math.max(1, Math.ceil(seconds / 60));
  if (m < 60) return `${m} min left`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min left`;
}

const two = (n: number) => String(n).padStart(2, "0");

/** ISO string or Date -> "21:52" (local time). */
export function hhmm(v: string | Date | null | undefined): string {
  if (!v) return "";
  const d = typeof v === "string" ? new Date(v) : v;
  if (Number.isNaN(d.getTime())) return "";
  return `${two(d.getHours())}:${two(d.getMinutes())}`;
}

/** Seconds -> "41 s", "3 min 12 s", "58 min", "1 h 05 min". */
export function duration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 && m < 10 ? `${m} min ${two(s % 60)} s` : `${m} min`;
  return `${Math.floor(m / 60)} h ${two(m % 60)} min`;
}

/** Seconds between two ISO strings (end defaults to now). */
export function secondsBetween(start: string | null, end?: string | null): number {
  if (!start) return 0;
  const a = new Date(start).getTime();
  const b = end ? new Date(end).getTime() : Date.now();
  return Math.max(0, (b - a) / 1000);
}

/** 28 -> "0:28", 4 -> "0:04". */
export function clock(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${two(s % 60)}`;
}

/** Whole numbers with a thin grouping, for file counts: 14 -> "14". */
export const count = (n: number): string => String(n);

/** "HH:MM" -> [hours, minutes], or null when it is not on the 24 hour clock. */
export function parseRunTime(v: string): [number, number] | null {
  const m = /^([01]\d|2[0-3]):([0-5]\d)$/.exec(v);
  return m ? [Number(m[1]), Number(m[2])] : null;
}

export const pct = (done: number, total: number | null): number => (total && total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0);

/** The time estimate of docs/design/installer-ui.md section 2.1: 50 Mbit/s for the download plus 15 minutes of installing. */
export function estimateFor(bytes: number): { downloadMin: number; installMin: number; label: string } {
  const downloadMin = Math.ceil((bytes * 8) / (50e6 * 60));
  const installMin = 15;
  const sum = downloadMin + installMin;
  let label: string;
  if (sum >= 50 && sum <= 75) label = "About 1 hour";
  else if (sum < 50) label = `About ${Math.max(5, Math.round(sum / 5) * 5)} minutes`;
  else label = `About ${Math.round((sum / 60) * 2) / 2} hours`;
  return { downloadMin, installMin, label };
}
