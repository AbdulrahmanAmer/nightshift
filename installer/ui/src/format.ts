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

/** Seconds -> "about 41 min", "about 1 h 05 min" (rounded up to the whole minute): the time left in the Downloading hero. */
export function aboutDuration(seconds: number): string {
  const m = Math.max(1, Math.ceil(seconds / 60));
  if (m < 60) return `about ${m} min`;
  return `about ${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
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

/** Free space a custom model needs: the base 60 GB plus what it is larger than Small's 9 GB (the same rule as plan.required_free_gb). */
export const customNeedFreeGb = (bytes: number): number => 60 + Math.max(0, Math.ceil(bytes / 1e9) - 9);
/** Video memory a 16 GB card has left for context: 16 GB minus the file minus 0.6 GB of runtime (the same rule as plan.vram_left_gb). */
export const vramLeftGb = (bytes: number): number => Math.round((16 - bytes / 1e9 - 0.6) * 10) / 10;

export const MAX_HOME_LEN = 120;
const RESERVED_NAMES = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i;
/** The model programs (llama.cpp, qwentts.cpp, CTranslate2) open their files through the ANSI code page. */
export const NON_ASCII_PATH = "Use a folder name with plain English letters, digits, spaces, - and _ (the model programs cannot open other paths)";
/**
 * Why a typed path cannot be the install folder, or null (the same rules as installer/stage2/web/pathcheck.py; the server
 * answers last). Spaces are fine, other than printable ASCII is not; the folder is made when Install starts, not before.
 */
export function homePathProblem(raw: string): string | null {
  const p = raw.trim();
  if (!p) return "Type a folder such as D:\\Nightshift, or press Browse";
  if (p.startsWith("\\\\") || p.startsWith("//")) return "Network folders are not supported. Pick a folder on a drive of this PC";
  if (!/^[A-Za-z]:\\/.test(p)) return "Use a full path such as D:\\Nightshift";
  const rest = p.slice(3);
  // eslint-disable-next-line no-control-regex
  if (/[<>:"|?*%\u0000-\u001f]/.test(rest)) return "That path has a character Windows does not allow: < > : \" | ? * %";
  if (p.length > MAX_HOME_LEN) return `Use a path of ${MAX_HOME_LEN} characters or fewer`;
  if (/[^ -~]/.test(p)) return NON_ASCII_PATH;
  const parts = rest.split("\\");
  if (parts[parts.length - 1] === "") parts.pop();
  for (const name of parts) {
    if (name === "") return "Use single backslashes in the path";
    if (name === "." || name === "..") return "Use a plain folder path without . or ..";
    if (name !== name.replace(/[ .]+$/, "") || RESERVED_NAMES.test(name.split(".")[0])) return `Windows does not allow the folder name "${name}"`;
  }
  return null;
}
