import { useEffect, useState } from "react";

/** Current time, re-rendering every `intervalMs` (Planify's useNow). Pass `fixed` to freeze it for screenshots. */
export function useNow(intervalMs = 1000, fixed?: Date): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (fixed) return;
    const id = window.setInterval(() => setNow(new Date()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs, fixed]);
  return fixed ?? now;
}

export const pad = (n: number, width = 2) => String(Math.max(0, Math.floor(n))).padStart(width, "0");
