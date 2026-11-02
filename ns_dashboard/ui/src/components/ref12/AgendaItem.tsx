import { cx } from "../../ui/kit";
import { Icon, type IconName } from "./Icon";
import { CardMenu, type MenuItem } from "./CardMenu";
import { IconButton } from "./IconButton";

export interface AgendaEvent { id: string; icon: IconName; title: string; time: string; tag: string; tagTone?: "accent" | "danger" | "success"; menu?: MenuItem[] }
const DOT = { accent: "bg-ds-accent", danger: "bg-ds-danger", success: "bg-ds-success" } as const;

/** AgendaItem (Planify): 34 px sunken icon tile (radius md, 16 px icon), sm/600 title, 2xs muted meta
 *  "time · dot tag" (5 px tone dot), vertical "…" right. 8 px vertical padding, 12 px gaps. */
export function AgendaItem({ event, moreLabel, onMore }: { event: AgendaEvent; moreLabel: string; onMore?: (id: string) => void }) {
  return (
    <li className="flex items-center gap-3 py-2">
      <span aria-hidden className="grid size-[34px] shrink-0 place-items-center rounded-ds-md bg-ds-sunken text-ds-text"><Icon name={event.icon} size={16} /></span>
      <div className="min-w-0 flex-1">
        <p className="truncate text-ds-sm font-semibold">{event.title}</p>
        <p className="flex items-center gap-1.5 text-ds-2xs text-ds-muted ds-num">
          {event.time}<span aria-hidden className={cx("size-[5px] shrink-0 rounded-full", DOT[event.tagTone ?? "accent"])} /><span className="truncate">{event.tag}</span>
        </p>
      </div>
      {event.menu ? <CardMenu items={event.menu} icon="moreVertical" label={moreLabel} /> : onMore ? <IconButton icon="moreVertical" label={moreLabel} size="sm" onClick={() => onMore(event.id)} /> : null}
    </li>
  );
}
