import { Pencil } from "lucide-react";
import { useId, type ReactNode } from "react";
import { cx } from "../../ui/kit";
import { ActionButton } from "./ActionButton";
import { Avatar, type AvatarData } from "./AvatarStack";
import { Card, CardHeader } from "./Card";
import { Chip } from "./Chip";
import { DashedAddButton } from "./DashedAddCard";
import { Kebab } from "./IconButton";
import { ICON, type Tone } from "./tokens";

export interface AssignmentData {
  /** Breadcrumb-like tags: the first renders in accent ink, the rest in ink ("Motion design  Logo"). */
  tags: string[];
  title: string;
  priority?: { label: string; tone?: Exclude<Tone, "ink"> };
  tag?: { label: string; tone?: Exclude<Tone, "ink"> };
  assignee?: AvatarData;
}

/** AssignmentItem: inner white row card, 16 px radius, 16 px padding, soft row shadow.
 *  Tags 12 medium; title 15/20 semibold (two lines max); priority = soft chip on the right;
 *  footer = solid tag chip left, assignee name 12 muted + 20 px avatar right. */
export function AssignmentItem({ data, moreLabel, className }: { data: AssignmentData; moreLabel: string; className?: string }) {
  return (
    <article className={cx("min-w-0 rounded-r12-row bg-r12-card p-4 shadow-r12-row", className)}>
      <div className="flex items-center justify-between gap-2">
        <p className="flex min-w-0 items-center gap-3 text-[12px] leading-4 font-medium">
          {data.tags.map((t, i) => <span key={t} className={cx("truncate", i === 0 ? "text-r12-accent-ink" : "text-r12-ink")}>{t}</span>)}
        </p>
        <Kebab label={moreLabel} className="-mr-2" />
      </div>
      <div className="mt-1.5 flex items-start justify-between gap-3">
        <h3 className="line-clamp-2 min-w-0 text-[15px] leading-5 font-semibold text-r12-ink">{data.title}</h3>
        {data.priority && <Chip tone={data.priority.tone ?? "red"} className="mt-1.5">{data.priority.label}</Chip>}
      </div>
      {(data.tag || data.assignee) && (
        <div className="mt-3 flex items-center justify-between gap-2">
          {data.tag ? <Chip tone={data.tag.tone ?? "green"} variant="solid" className="h-7 px-4">{data.tag.label}</Chip> : <span />}
          {data.assignee && <span className="flex min-w-0 items-center gap-2 text-[12px] text-r12-muted"><span className="truncate">{data.assignee.name}</span><Avatar {...data.assignee} size={20} ring={false} /></span>}
        </div>
      )}
    </article>
  );
}

/** AssignmentsCard: card header with "Edit", one or more AssignmentItems, then a DashedAddButton. */
export function AssignmentsCard({ title, editLabel, onEdit, items, moreLabel, addLabel, onAdd, footer, className }: {
  title: string; editLabel?: string; onEdit?: () => void; items: AssignmentData[]; moreLabel: string; addLabel?: string; onAdd?: () => void; footer?: ReactNode; className?: string;
}) {
  const id = useId();
  return (
    <Card labelledBy={id} className={className}>
      <CardHeader id={id} title={title} action={editLabel && <ActionButton variant="ghost" size="sm" onClick={onEdit} icon={<Pencil size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden />}>{editLabel}</ActionButton>} />
      <div className="mt-4 flex flex-col gap-4">
        {items.map((it) => <AssignmentItem key={it.title} data={it} moreLabel={moreLabel} />)}
        {addLabel && <DashedAddButton label={addLabel} onClick={onAdd} />}
        {footer}
      </div>
    </Card>
  );
}
