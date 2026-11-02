import { CalendarDays, Clock } from "lucide-react";
import type { ReactNode } from "react";
import { cx } from "../../ui/kit";
import { Dot } from "./Chip";
import { IconButton, Kebab } from "./IconButton";
import { ICON, type Tone } from "./tokens";

export interface SwipeAction { label: string; icon: ReactNode; onClick?: () => void }

/** FloatingEventCard: the raised "Upcoming event" card. It is a list row shown in its swiped state:
 *  the card slides 44 px left of the list edge (20 px past the parent card), lifts onto the float layer,
 *  and reveals a tint action tray (delete, edit) behind it.
 *  Card: 20 px radius, 16/20 padding, float shadow. Title 15/20 semibold + 6 px status dot; meta 12 muted with a
 *  hairline divider; info row 12 medium text-2 with 14 px calendar and clock icons. */
export function FloatingEventCard({ title, statusTone = "green", statusLabel, meta, date, time, actions = [], moreLabel, revealed = true, className }: {
  title: string; statusTone?: Tone; statusLabel?: string; meta: string[]; date?: string; time?: string;
  actions?: SwipeAction[]; moreLabel: string; revealed?: boolean; className?: string;
}) {
  return (
    <div className={cx("relative min-w-0", className)}>
      {actions.length > 0 && (
        <div className="absolute inset-0 flex flex-col items-end justify-center gap-1 rounded-[20px] bg-r12-tint-3 pr-2">
          {actions.map((a) => <IconButton key={a.label} label={a.label} size={32} round onClick={a.onClick} className="text-r12-ink">{a.icon}</IconButton>)}
        </div>
      )}
      <article className={cx("relative rounded-[20px] bg-r12-card px-5 py-4 shadow-r12-float transition-transform duration-300 ease-[var(--ease-out)]",
        revealed && actions.length ? "mr-12 -ml-2 sm:-ml-11" : "")}>
        <div className="flex items-start justify-between gap-2">
          <h3 className="flex min-w-0 items-center gap-1.5 text-[15px] leading-5 font-semibold text-r12-ink">
            <span className="truncate">{title}</span><Dot tone={statusTone} />{statusLabel && <span className="sr-only">{statusLabel}</span>}
          </h3>
          <Kebab label={moreLabel} className="-mt-1 -mr-2" />
        </div>
        {meta.length > 0 && (
          <p className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 text-[12px] leading-4 text-r12-muted">
            {meta.map((m, i) => <span key={i} className={cx("whitespace-nowrap", i > 0 && "border-l border-r12-line-2 pl-2")}>{m}</span>)}
          </p>
        )}
        {(date || time) && (
          <p className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-[12px] leading-4 font-medium text-r12-text-2">
            {date && <span className="inline-flex items-center gap-1.5"><CalendarDays size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden />{date}</span>}
            {time && <span className="inline-flex items-center gap-1.5 num"><Clock size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden />{time}</span>}
          </p>
        )}
      </article>
    </div>
  );
}
