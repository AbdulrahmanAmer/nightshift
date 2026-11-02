import { useId } from "react";
import { Button } from "./Button";
import { Card } from "./Card";
import { CardHeader } from "./CardHeader";

/** MeetingCard (Planify, copy via props): padding md card. Header with a ghost Edit (4 px below),
 *  a 2xs muted "when" line led by an 8 px accent dot, then the xs description (max 24ch) and a
 *  soft + primary button pair, bottom-aligned and wrapping. */
export function MeetingCard({ title, when, description, editLabel, onEdit, secondaryLabel, onSecondary, primaryLabel, onPrimary, className }: {
  title: string; when: string; description: string; editLabel?: string; onEdit?: () => void;
  secondaryLabel?: string; onSecondary?: () => void; primaryLabel: string; onPrimary?: () => void; className?: string;
}) {
  const id = useId();
  return (
    <Card as="section" padding="md" aria-labelledby={id} className={className}>
      <CardHeader id={id} title={title} className="!mb-1" actions={editLabel && <Button variant="ghost" size="sm" leadingIcon="edit" onClick={onEdit}>{editLabel}</Button>} />
      <p className="flex items-center gap-2 text-ds-2xs text-ds-muted ds-num"><span aria-hidden className="size-2 rounded-full bg-ds-accent" />{when}</p>
      <div className="mt-3 flex flex-wrap items-end justify-between gap-3">
        <p className="max-w-[24ch] text-ds-xs leading-ds-snug">{description}</p>
        <div className="flex gap-2">
          {secondaryLabel && <Button variant="soft" onClick={onSecondary}>{secondaryLabel}</Button>}
          <Button onClick={onPrimary}>{primaryLabel}</Button>
        </div>
      </div>
    </Card>
  );
}
