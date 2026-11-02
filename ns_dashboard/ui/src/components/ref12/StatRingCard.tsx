import { cx } from "../../ui/kit";
import { ActionButton } from "./ActionButton";
import { RingProgress } from "./RingProgress";
import { TONE_TEXT, type Tone } from "./tokens";

/** StatRingCard: compact card (16 px padding, 28 px radius). Row 1: RingProgress 48 + eyebrow (12 medium, tone ink)
 *  over a 15 px semibold title. Row 2: two 12/16 muted meta lines, optional small accent button bottom-right. */
export function StatRingCard({ value, tone = "green", eyebrow, title, meta, actionLabel, onAction, className }: {
  value: number; tone?: Tone; eyebrow: string; title: string; meta: [string, string?]; actionLabel?: string; onAction?: () => void; className?: string;
}) {
  return (
    <article className={cx("r12-card flex min-w-0 flex-col justify-between gap-2 p-4", className)}>
      <div className="flex items-center gap-3">
        <RingProgress value={value} tone={tone} label={title} />
        <div className="min-w-0">
          <p className={cx("truncate text-[12px] leading-4 font-medium", TONE_TEXT[tone])}>{eyebrow}</p>
          <h3 className="truncate text-[15px] leading-5 font-semibold text-r12-ink">{title}</h3>
        </div>
      </div>
      <div className="flex items-end justify-between gap-2">
        <p className="min-w-0 text-[12px] leading-4 text-r12-muted num">{meta[0]}{meta[1] && <><br />{meta[1]}</>}</p>
        {actionLabel && <ActionButton variant="accent" size="sm" onClick={onAction}>{actionLabel}</ActionButton>}
      </div>
    </article>
  );
}
