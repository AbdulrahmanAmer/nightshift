/* A failed job: the red card with the code, what to do (result.error.hint) and a Diagnostics button. */
import { useState } from "react";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { stageWords } from "../lib/format";
import type { Job } from "../lib/types";
import { Button, Icon } from "../components/ref12";

export function JobError({ j }: { j: Job }) {
  const { toast } = useApp(); const [busy, setBusy] = useState(false);
  const code = j.result?.error?.code; const hint = j.result?.error?.hint;
  const report = async () => { setBusy(true); try { const r = await api.post("/diagnostics"); toast(`Report saved to ${r?.path || "your Desktop"}`, "ok"); } catch (e: any) { toast(e.message, "bad"); } setBusy(false); };
  return (
    <div role="alert" className="mt-2 flex flex-col gap-2 rounded-ds-md bg-ds-danger-soft px-3 py-3 text-ds-sm">
      <p className="flex items-start gap-2 font-semibold"><Icon name="alert" size={16} className="mt-0.5 shrink-0 text-ds-danger" />Stopped at {stageWords(j.stage).toLowerCase()}{code && code !== "job_failed" && code !== "unavailable" ? <span className="font-mono text-ds-2xs">{code}</span> : null}</p>
      {j.message && <p className="text-ds-text-2">{j.message}</p>}
      {hint && <p><span className="font-semibold">What to do: </span>{hint}</p>}
      <div><Button size="sm" variant="soft" leadingIcon="archive" disabled={busy} onClick={report}>Make a diagnostics report</Button></div>
    </div>
  );
}
