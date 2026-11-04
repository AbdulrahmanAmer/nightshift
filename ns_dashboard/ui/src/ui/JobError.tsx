/* A failed job: the red card with the code, what to do (result.error.hint) and a Diagnostics button. */
import { useState } from "react";
import { api } from "../lib/api";
import { useApp } from "../lib/app";
import { stoppedAt } from "../lib/format";
import type { Job } from "../lib/types";
import { Button, Icon } from "../components/ref12";

export function JobError({ j }: { j: Job }) {
  const { toast, refresh } = useApp(); const [busy, setBusy] = useState(false); const [resuming, setResuming] = useState(false);
  const code = j.result?.error?.code; const hint = j.result?.error?.hint;
  const studioIds: string[] = [...(j.result?.studio_job_ids || []), ...(j.result?.studio_job_id ? [j.result.studio_job_id] : [])];
  const report = async () => { setBusy(true); try { const r = await api.post("/diagnostics"); toast(`Report saved to ${r?.path || "your Desktop"}`, "ok"); } catch (e: any) { toast(e.message, "bad"); } setBusy(false); };
  /** A story job that stopped late (voice, clips, render) continues from that stage: same story, nothing written again. */
  const canResume = j.kind === "run" && (j.result?.studio_job_ids || []).length > 0;
  const resume = async () => { setResuming(true); try { await api.post(`/jobs/${encodeURIComponent(j.job_id)}/resume`); toast("Continuing from where it stopped. Follow it in the Queue", "ok"); refresh(); } catch (e: any) { toast(e.message, "bad"); } setResuming(false); };
  return (
    <div role="alert" className="mt-2 flex flex-col gap-2 rounded-ds-md bg-ds-danger-soft px-3 py-3 text-ds-sm">
      <p className="flex items-start gap-2 font-semibold"><Icon name="alert" size={16} className="mt-0.5 shrink-0 text-ds-danger" />{stoppedAt(j.stage)}{code && code !== "job_failed" && code !== "unavailable" ? <span className="font-mono text-ds-2xs">{code}</span> : null}</p>
      {j.message && <p className="text-ds-text-2">{j.message}</p>}
      {hint && <p><span className="font-semibold">What to do: </span>{hint}</p>}
      {studioIds.length > 0 && <p className="text-ds-2xs text-ds-muted">Logs of the failed run: <span className="font-mono">jobs/{studioIds[studioIds.length - 1]}</span></p>}
      <div className="flex flex-wrap items-center gap-2">
        {canResume && <Button size="sm" variant="soft" leadingIcon="refresh" disabled={resuming} onClick={resume}>{resuming ? "Starting" : "Retry from where it stopped"}</Button>}
        <Button size="sm" variant="soft" leadingIcon="archive" disabled={busy} onClick={report}>Make a diagnostics report</Button>
      </div>
    </div>
  );
}
