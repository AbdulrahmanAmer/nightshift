import { cx } from "../../ui/kit";
import { Avatar, type Person } from "./Avatar";
import { Badge, type Tone } from "./Badge";
import { CardMenu, type MenuItem } from "./CardMenu";
import { IconButton } from "./IconButton";

/** AssignmentCard (Planify, copy via props): radius lg surface tile with a hairline ring, padding 12/12/16/16.
 *  Head: first category as a soft accent Badge, the rest as 2xs/600 text, "…" right.
 *  Main: md/600 title (snug) with a soft md priority Badge. Foot: solid success md tag Badge and the assignee
 *  (2xs secondary name + 18 px avatar). */
export function AssignmentCard({ categories, title, priority, tag, assignee, moreLabel, onMore, menu, className }: {
  categories: string[]; title: string; priority: { label: string; tone?: Tone }; tag: string; assignee: Person; moreLabel: string; onMore?: () => void; menu?: MenuItem[]; className?: string;
}) {
  const [discipline, ...rest] = categories;
  return (
    <article className={cx("rounded-ds-lg bg-ds-surface pt-3 pr-3 pb-4 pl-4 shadow-[0_0_0_1px_var(--ds-border-subtle)]", className)}>
      <header className="flex items-center justify-between">
        <div className="-ml-2 flex min-w-0 items-center gap-3">
          {discipline && <Badge tone="accent">{discipline}</Badge>}
          {rest.map((c) => <span key={c} className="truncate text-ds-2xs font-semibold">{c}</span>)}
        </div>
        {menu ? <CardMenu items={menu} label={moreLabel} /> : onMore ? <IconButton icon="more" label={moreLabel} size="sm" onClick={onMore} /> : null}
      </header>
      <div className="mt-3 flex items-start justify-between gap-4">
        <h3 className="min-w-0 text-ds-md leading-ds-snug font-semibold">{title}</h3>
        <Badge tone={priority.tone ?? "danger"} size="md" className="mt-1">{priority.label}</Badge>
      </div>
      <footer className="mt-4 flex items-center justify-between gap-3">
        <Badge tone="success" variant="solid" size="md">{tag}</Badge>
        <span className="inline-flex min-w-0 items-center gap-2 text-ds-2xs text-ds-text-2"><span className="truncate">{assignee.name}</span><Avatar {...assignee} size="xs" /></span>
      </footer>
    </article>
  );
}
