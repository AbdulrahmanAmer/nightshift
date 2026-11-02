import { Pencil } from "lucide-react";
import { useId } from "react";
import { ActionButton } from "./ActionButton";
import { Card, CardHeader } from "./Card";
import { ICON } from "./tokens";

/** BoardMeetingCard: 22/28 semibold title with Edit, a date line (10 px light-accent dot + 12 px muted),
 *  then a two-line 14/20 description on the left and a soft + accent button pair on the right
 *  (32 px buttons, 8 px apart). Wraps the buttons under the text below 480 px. */
export function BoardMeetingCard({ title, date, description, editLabel, onEdit, secondaryLabel, onSecondary, primaryLabel, onPrimary, className }: {
  title: string; date: string; description: string; editLabel?: string; onEdit?: () => void;
  secondaryLabel?: string; onSecondary?: () => void; primaryLabel: string; onPrimary?: () => void; className?: string;
}) {
  const id = useId();
  return (
    <Card labelledBy={id} className={className}>
      <CardHeader id={id} size="lg" title={title} action={editLabel && <ActionButton variant="ghost" size="sm" onClick={onEdit} icon={<Pencil size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden />}>{editLabel}</ActionButton>} />
      <p className="mt-1 flex items-center gap-2 text-[12px] leading-4 text-r12-muted num"><span aria-hidden className="size-2.5 rounded-full bg-r12-accent-2" />{date}</p>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-x-3 gap-y-3">
        <p className="max-w-[180px] min-w-0 flex-1 basis-[120px] text-[13px] leading-[18px] text-r12-text-2">{description}</p>
        <div className="flex gap-2">
          {secondaryLabel && <ActionButton variant="soft" className="h-8" onClick={onSecondary}>{secondaryLabel}</ActionButton>}
          <ActionButton variant="accent" className="h-8" onClick={onPrimary}>{primaryLabel}</ActionButton>
        </div>
      </div>
    </Card>
  );
}
