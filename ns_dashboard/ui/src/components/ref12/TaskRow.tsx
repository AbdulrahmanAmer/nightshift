import { cx } from "../../ui/kit";
import type { IconName } from "./Icon";
import { MetaItem } from "./MetaItem";
import { ProgressBar } from "./ProgressBar";
import type { Tone } from "./Badge";

export interface TaskMeta { icon: IconName; value: string | number; label: string }
export interface Task { id: string; title: string; date: string; metricLabel: string; metric: string; progress: number; progressLabel: string; tone?: Tone; meta: TaskMeta[] }

/** TaskRow (Planify layout, generic columns): grid 1.3fr / 0.8fr / 1fr / auto with 24 px gaps, radius lg,
 *  padding 12/16/12/8. Title sm/600 over a 2xs muted date; metric 2xs muted label over sm/600 value;
 *  ProgressBar (track max 56); meta = MetaItems split by a 4 px ink dot. Hover = hover fill;
 *  highlighted = surface + elevation 2, pulled 8 px left. Under 448 px of row width it wraps to two lines. */
export function TaskRow({ task, highlighted, onSelect }: { task: Task; highlighted?: boolean; onSelect?: (id: string) => void }) {
  return (
    <li aria-current={highlighted || undefined} className="@container">
      <div className={cx(
        "grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-6 gap-y-2 rounded-ds-lg py-3 pr-4 pl-2 transition-[background-color,box-shadow] duration-200 ease-ds-out @md:grid-cols-[minmax(120px,1.3fr)_minmax(70px,0.8fr)_minmax(90px,1fr)_auto]",
        highlighted ? "-ml-2 bg-ds-surface pl-4 shadow-ds-2" : "hover:bg-ds-hover",
      )}>
        <div className="flex min-w-0 flex-col gap-0.5">
          <button type="button" onClick={() => onSelect?.(task.id)} className="ds-focus truncate rounded-ds-xs text-left text-ds-sm font-semibold">{task.title}</button>
          <span className="truncate text-ds-2xs text-ds-muted ds-num">{task.date}</span>
        </div>
        <div className="order-3 flex min-w-0 flex-col gap-0.5 @md:order-none">
          <span className="text-ds-2xs text-ds-muted">{task.metricLabel}</span>
          <span className="text-ds-sm font-semibold whitespace-nowrap ds-num">{task.metric}</span>
        </div>
        <ProgressBar value={task.progress} tone={task.tone} label={task.progressLabel} maxTrack={56} className="order-4 @md:order-none" />
        <div className="order-2 inline-flex items-center justify-end gap-2 @md:order-none">
          {task.meta.map((m, i) => (
            <span key={m.label} className="inline-flex items-center gap-2">
              {i > 0 && <span aria-hidden className="size-1 rounded-full bg-ds-text" />}
              <MetaItem icon={m.icon} className="!text-ds-text"><span>{m.value}</span><span className="sr-only"> {m.label}</span></MetaItem>
            </span>
          ))}
        </div>
      </div>
    </li>
  );
}
