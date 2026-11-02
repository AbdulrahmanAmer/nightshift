import { ChevronLeft, ChevronRight } from "lucide-react";
import { useId } from "react";
import { cx } from "../../ui/kit";
import { AgendaTimeline, type AgendaSlot } from "./AgendaTimeline";
import { Card, CardHeader } from "./Card";
import { IconButton } from "./IconButton";
import { ICON } from "./tokens";

export interface WeekDay { weekday: string; day: number | string; label: string; marked?: boolean }

/** WeekStrip: 7 equal columns. Weekday 12 px muted, day 13 px semibold ink; the active day is a
 *  28 px accent circle with white text and a soft accent shadow. Marked days get a 4 px dot below. */
export function WeekStrip({ days, active, onPick, className }: { days: WeekDay[]; active?: number | string; onPick?: (d: WeekDay) => void; className?: string }) {
  return (
    <div role="group" className={cx("grid grid-cols-7 gap-1 text-center", className)}>
      {days.map((d) => {
        const on = d.day === active;
        return (
          <button key={d.label} type="button" aria-label={d.label} aria-pressed={on} onClick={() => onPick?.(d)}
            className="r12-focus group flex flex-col items-center gap-1 rounded-[12px]">
            <span className="text-[12px] leading-4 text-r12-muted">{d.weekday}</span>
            <span className={cx("relative flex size-7 items-center justify-center rounded-full text-[13px] font-semibold num transition-colors duration-150",
              on ? "bg-r12-accent text-r12-on-accent shadow-[0_6px_14px_-4px_var(--r12-accent)]" : "text-r12-ink group-hover:bg-r12-tint-2")}>
              {d.day}
              {d.marked && !on && <span aria-hidden className="absolute bottom-0.5 size-1 rounded-full bg-r12-accent" />}
            </span>
          </button>
        );
      })}
    </div>
  );
}

/** CalendarCard: month title + round prev/next (28 px, tint fill), WeekStrip, then an AgendaTimeline. */
export function CalendarCard({ month, prevLabel, nextLabel, onPrev, onNext, days, active, onPick, slots, moreLabel, className }: {
  month: string; prevLabel: string; nextLabel: string; onPrev?: () => void; onNext?: () => void;
  days: WeekDay[]; active?: number | string; onPick?: (d: WeekDay) => void; slots: AgendaSlot[]; moreLabel: string; className?: string;
}) {
  const id = useId();
  return (
    <Card labelledBy={id} className={className}>
      <CardHeader id={id} title={month} action={<>
        <IconButton label={prevLabel} size={28} round onClick={onPrev} className="bg-r12-tint"><ChevronLeft size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden /></IconButton>
        <IconButton label={nextLabel} size={28} round onClick={onNext} className="bg-r12-tint"><ChevronRight size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden /></IconButton>
      </>} />
      <WeekStrip days={days} active={active} onPick={onPick} className="mt-1" />
      <AgendaTimeline slots={slots} moreLabel={moreLabel} className="mt-3" />
    </Card>
  );
}
