// List rows: CheckRowItem (hardware check), VerifyRowItem (test checks), StepItem (install and failed steps).
// docs/design/installer-ui.md sections 2.2, 2.5, 2.6, 2.8. Rows sit 4 px apart; the live dot is only on a row that is working.
import { useEffect, useState, type ReactNode } from "react";
import { Badge, StatusDot, type DotTone } from "@ref12";
import { cx } from "@dash/ui/kit";
import type { CheckRow, StepView, VerifyRow } from "../api/types";
import { checks as checksCopy, installing as installCopy, stepTitle as stepTitles, verify as verifyCopy } from "../copy";
import { duration, secondsBetween } from "../format";

/** An empty 8 px ring, the "not started" mark. */
export function Ring() { return <span aria-hidden className="inline-block size-2 shrink-0 rounded-full border border-ds-border" />; }

/* ------------------------------------------------------------------ hardware check */

const CHECK_DOT = { ok: "success", warn: "attention", fail: "danger" } as const;
const CHECK_BADGE = { ok: "success", warn: "neutral", fail: "danger" } as const;

export function CheckRowItem({ row, checking, onActivate }: { row: CheckRow; checking?: boolean; onActivate?: () => void }) {
  const inner = (
    <>
      <StatusDot tone={CHECK_DOT[row.status]} size={8} />
      <span className="w-[200px] shrink-0 truncate text-ds-sm font-medium">{row.label}</span>
      <span className="min-w-0 flex-1 truncate text-ds-sm text-ds-text-2 ds-num">{row.value}</span>
      <Badge tone={CHECK_BADGE[row.status]} variant="soft" size="sm">{checksCopy.badge[row.status]}</Badge>
    </>
  );
  const cls = cx("flex h-10 w-full items-center gap-4 rounded-ds-md px-3 text-left transition-[background-color,opacity] duration-[120ms] ease-ds-out", checking && "opacity-60");
  return (
    <li>
      {row.status !== "ok" && onActivate
        ? <button type="button" onClick={onActivate} className={cx(cls, "ds-focus hover:bg-ds-hover")}>{inner}</button>
        : <div className={cx(cls, "hover:bg-ds-hover")}>{inner}</div>}
    </li>
  );
}

/* ------------------------------------------------------------------ verify */

const V_DOT: Record<VerifyRow["status"], DotTone | "ring"> = { passed: "success", fallback: "attention", failed: "danger", running: "online", pending: "ring" };
const V_BADGE: Record<VerifyRow["status"], "success" | "neutral" | "danger" | null> = { passed: "success", fallback: "neutral", failed: "danger", running: null, pending: null };

export function VerifyRowItem({ label, status, value }: { label: string; status: VerifyRow["status"]; value: string | null }) {
  const dot = V_DOT[status]; const tone = V_BADGE[status];
  return (
    <li className="flex min-h-10 items-center gap-3 rounded-ds-md px-2">
      <span className="grid size-2 shrink-0 place-items-center">{dot === "ring" ? <Ring /> : <StatusDot tone={dot} size={8} />}</span>
      <span className="w-[132px] shrink-0 py-1 text-ds-sm font-medium">{label}</span>
      <span className="min-w-0 flex-1 text-ds-sm text-ds-text-2 ds-num">{status === "running" ? "..." : value ?? ""}</span>
      {tone ? <Badge tone={tone} variant="soft" size="sm">{verifyCopy.statusBadge[status]}</Badge> : <span className="h-[22px]" />}
    </li>
  );
}

/* ------------------------------------------------------------------ steps */

/** Re-renders every second while mounted: only used by the one running row. */
function useSecond(active: boolean) {
  const [, set] = useState(0);
  useEffect(() => { if (!active) return; const t = window.setInterval(() => set((n) => n + 1), 1000); return () => window.clearInterval(t); }, [active]);
}

function StepStatus({ step }: { step: StepView }) {
  const running = step.status === "running";
  useSecond(running);
  if (step.status === "done") return <>{installCopy.done}{step.startedAt && step.finishedAt ? ` · ${duration(secondsBetween(step.startedAt, step.finishedAt))}` : ""}</>;
  if (running) return <>{installCopy.running} · {duration(secondsBetween(step.startedAt))}</>;
  if (step.status === "skipped") return <>{installCopy.skipped}</>;
  if (step.status === "failed") return <>{installCopy.failed}</>;
  if (step.status === "warning") return <>{installCopy.done}</>;
  return <>{installCopy.waiting}</>;
}

const STEP_DOT: Record<StepView["status"], DotTone | "ring"> = { done: "success", running: "online", failed: "danger", warning: "attention", skipped: "ring", pending: "ring" };

/** 44 px row on Installing (with the running step's latest message), 32 px compact row on Failed. */
export function StepItem({ step, compact, current }: { step: StepView; compact?: boolean; current?: boolean }) {
  const dot = STEP_DOT[step.status];
  const title = step.title || stepTitles[step.id];
  const mark = dot === "ring" ? <Ring /> : <StatusDot tone={dot} size={8} />;
  if (compact) {
    return (
      <li className={cx("flex h-8 items-center gap-3 rounded-ds-md px-2 text-ds-sm", step.status === "pending" && "text-ds-text-2", current && "font-medium")} aria-current={current ? "step" : undefined}>
        <span className="grid size-2 shrink-0 place-items-center">{mark}</span>
        <span className="min-w-0 flex-1 truncate">{title}</span>
        <span className="sr-only"><StepStatus step={step} /></span>
      </li>
    );
  }
  return (
    <li className="flex min-h-11 items-start gap-4 rounded-ds-md px-3 py-3">
      <span className="mt-[5px] grid size-2 shrink-0 place-items-center">{mark}</span>
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline justify-between gap-4">
          <span className="truncate text-ds-sm font-medium">{title}</span>
          <span className="shrink-0 text-ds-xs text-ds-text-2 ds-num"><StepStatus step={step} /></span>
        </div>
        {step.status === "running" && step.detail && <p className="mt-0.5 text-ds-xs text-ds-text-2">{step.detail}</p>}
      </div>
    </li>
  );
}

export function RowList({ children, label }: { children: ReactNode; label: string }) {
  return <ul aria-label={label} className="flex flex-col gap-1">{children}</ul>;
}
