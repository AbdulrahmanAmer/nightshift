import { cx } from "../../ui/kit";
import { Button } from "./Button";
import { Card } from "./Card";
import { ProgressRing } from "./ProgressRing";

const EYEBROW = { success: "text-ds-success-ink", danger: "text-ds-danger", accent: "text-ds-accent-ink" } as const;

/** StatCard (Planify): padding md card. Head: 48 px ProgressRing + tone eyebrow (2xs/600, sentence case) over an md/600 title.
 *  Foot: two 2xs muted caption lines and an optional sm primary button, 16 px below the head. */
export function StatCard({ value, tone, eyebrow, title, caption, detail, action, className }: {
  value: number; tone: keyof typeof EYEBROW; eyebrow: string; title: string; caption: string; detail: string; action?: { label: string; onClick?: () => void }; className?: string;
}) {
  return (
    <Card as="article" padding="md" className={cx("flex flex-col gap-4", className)}>
      <div className="flex items-center gap-3">
        <ProgressRing value={value} tone={tone} size={48} label={title} />
        <div className="flex min-w-0 flex-col">
          <span className={cx("text-ds-2xs font-semibold whitespace-nowrap", EYEBROW[tone])}>{eyebrow}</span>
          <h3 className="truncate text-ds-md leading-ds-snug font-semibold">{title}</h3>
        </div>
      </div>
      <div className="flex items-end justify-between gap-2">
        <p className="text-ds-2xs leading-ds-snug text-ds-muted ds-num">{caption}<br />{detail}</p>
        {action && <Button size="sm" onClick={action.onClick}>{action.label}</Button>}
      </div>
    </Card>
  );
}
