import type { ReactNode } from "react";
import { cx } from "../../ui/kit";
import { ProgressBar } from "./ProgressBar";
import type { Tone } from "./tokens";

export interface TaskCount { icon: ReactNode; value: string | number; label: string }
export interface TaskData { id: string; name: string; when: string; metricLabel: string; metricValue: string; progress: number; progressLabel: string; tone?: Tone; counts?: TaskCount[] }

/** TaskRow: one row of Today tasks. 64 px tall (12/16 padding), 16 px radius.
 *  Columns (container query, at 448 px and wider): name+date | metric label+value | progress | counts.
 *  Narrower: name+date and counts on line one, metric and progress on line two.
 *  Name 14 semibold ink, date 12 muted; metric label 11 muted, value 14 semibold tabular;
 *  counts = 14 px muted icon + 12 semibold value separated by a 3 px dot. Raised = white + row shadow. */
export function TaskRow({ task, raised, className }: { task: TaskData; raised?: boolean; className?: string }) {
  return (
    <li className={cx("@container rounded-r12-row transition-[background-color,box-shadow] duration-150 hover:bg-r12-card hover:shadow-r12-row", raised && "bg-r12-card shadow-r12-row", className)}>
      <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-2 px-4 py-3 @md:grid-cols-[minmax(0,1.35fr)_minmax(0,0.9fr)_minmax(0,1.1fr)_auto]">
        <div className="min-w-0">
          <p className="truncate text-[14px] leading-5 font-semibold text-r12-ink">{task.name}</p>
          <p className="truncate text-[12px] leading-4 text-r12-muted num">{task.when}</p>
        </div>
        <div className="order-3 min-w-0 @md:order-none">
          <p className="text-[11px] leading-4 text-r12-muted">{task.metricLabel}</p>
          <p className="text-[14px] leading-5 font-semibold text-r12-ink num">{task.metricValue}</p>
        </div>
        <ProgressBar value={task.progress} label={task.progressLabel} tone={task.tone} className="order-4 justify-self-end @md:order-none @md:justify-self-start" />
        <div className="order-2 flex items-center justify-end gap-2 text-[12px] font-semibold text-r12-ink @md:order-none">
          {task.counts?.map((c, i) => (
            <span key={c.label} className="inline-flex items-center gap-2">
              {i > 0 && <span aria-hidden className="size-[3px] rounded-full bg-r12-ink" />}
              <span className="inline-flex items-center gap-1 num" title={c.label}><span aria-hidden className="text-r12-muted">{c.icon}</span>{c.value}<span className="sr-only"> {c.label}</span></span>
            </span>
          ))}
        </div>
      </div>
    </li>
  );
}
