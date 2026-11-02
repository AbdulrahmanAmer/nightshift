import type { ReactNode } from "react";
import { cx } from "../../ui/kit";
import { Dot } from "./Chip";
import { Kebab } from "./IconButton";
import type { Tone } from "./tokens";

export interface AgendaEvent { title: string; icon: ReactNode; time: string; note?: string; tone?: Tone }
export interface AgendaSlot { label: string; events: AgendaEvent[] }

/** AgendaSlotLabel: 14 px semibold time range followed by a dotted rule to the right edge (2 px dots, 6 px pitch). */
export function AgendaSlotLabel({ children }: { children: ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <span className="shrink-0 text-[14px] leading-5 font-semibold text-r12-ink num">{children}</span>
      <span aria-hidden className="r12-dots h-1 flex-1" />
    </div>
  );
}

/** AgendaEventRow: 28 px tint icon tile (8 px radius, 16 px icon), 14 px semibold title, 12 px muted meta
 *  "time · dot note", vertical kebab on the right. Row height 48. */
export function AgendaEventRow({ event, moreLabel }: { event: AgendaEvent; moreLabel: string }) {
  return (
    <div className="flex min-h-12 items-center gap-3">
      <span aria-hidden className="flex size-8 shrink-0 items-center justify-center rounded-[8px] bg-r12-tint-2 text-r12-ink">{event.icon}</span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-[14px] leading-5 font-semibold text-r12-ink">{event.title}</p>
        <p className="flex min-w-0 items-center gap-1.5 text-[12px] leading-4 text-r12-muted">
          <span className="shrink-0 num">{event.time}</span>
          {event.note && <><Dot tone={event.tone ?? "green"} /><span className="truncate">{event.note}</span></>}
        </p>
      </div>
      <Kebab vertical label={moreLabel} className="-mr-2" />
    </div>
  );
}

/** AgendaTimeline: slots stacked 12 px apart; 4 px between a slot label and its events. */
export function AgendaTimeline({ slots, moreLabel, className }: { slots: AgendaSlot[]; moreLabel: string; className?: string }) {
  return (
    <ol className={cx("flex flex-col gap-3", className)}>
      {slots.map((s) => (
        <li key={s.label} className="flex flex-col gap-1">
          <AgendaSlotLabel>{s.label}</AgendaSlotLabel>
          {s.events.map((e) => <AgendaEventRow key={e.title} event={e} moreLabel={moreLabel} />)}
        </li>
      ))}
    </ol>
  );
}
