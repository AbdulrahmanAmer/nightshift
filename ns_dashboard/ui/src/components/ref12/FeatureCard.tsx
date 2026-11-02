import type { ReactNode } from "react";
import { cx } from "../../ui/kit";

/** FeatureCard (Planify): raised tile (elevation 2, radius xl, padding 16/20) with a spot illustration
 *  (max 120 px wide) over an md/600 title and a 2-line 2xs muted description. As a link or button it lifts on hover. */
export function FeatureCard({ illustration, title, description, href, onClick, className }: { illustration: ReactNode; title: string; description?: string; href?: string; onClick?: () => void; className?: string }) {
  const cls = cx(
    "ds-focus flex min-w-0 flex-col rounded-ds-xl bg-ds-surface px-5 py-4 text-left text-ds-text no-underline shadow-ds-2 transition-[box-shadow,transform] duration-200 ease-ds-out",
    (href || onClick) && "cursor-pointer hover:-translate-y-0.5", className,
  );
  const body = (
    <>
      <span aria-hidden className="grid flex-1 place-items-center px-4 pt-2 pb-3 [&>svg]:max-w-[120px]">{illustration}</span>
      <span className="block text-ds-md leading-ds-snug font-semibold">{title}</span>
      {description && <span className="mt-0.5 line-clamp-2 text-ds-2xs leading-ds-snug text-ds-muted">{description}</span>}
    </>
  );
  if (href) return <a href={href} className={cls}>{body}</a>;
  if (onClick) return <button type="button" onClick={onClick} className={cls}>{body}</button>;
  return <article className={cls}>{body}</article>;
}
