import type { ReactNode } from "react";
import { cx } from "../../ui/kit";

/** FeatureCard: raised square tile with a line-art illustration, a 15 px title and a 12 px faint subtitle.
 *  24 px radius, 16 px padding, raised shadow; lifts 2 px on hover. Rendered as a link or a button. */
export function FeatureCard({ title, subtitle, illustration, href, onClick, className }: {
  title: string; subtitle?: string; illustration: ReactNode; href?: string; onClick?: () => void; className?: string;
}) {
  const body = (
    <>
      <span aria-hidden className="flex min-h-0 flex-1 items-center justify-center [&>svg]:h-full [&>svg]:max-h-[108px] [&>svg]:w-auto">{illustration}</span>
      <span className="mt-2 block truncate text-[15px] leading-5 font-semibold text-r12-ink">{title}</span>
      {subtitle && <span className="mt-0.5 block truncate text-[12px] leading-4 text-r12-faint">{subtitle}</span>}
    </>
  );
  const cls = cx("r12-focus r12-lift flex aspect-square w-full min-w-0 flex-col rounded-r12-tile bg-r12-card-2 p-4 text-left no-underline shadow-r12-raised", className);
  return href ? <a href={href} className={cls}>{body}</a> : <button type="button" onClick={onClick} className={cls}>{body}</button>;
}
