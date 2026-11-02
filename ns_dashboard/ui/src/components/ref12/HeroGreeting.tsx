import type { ReactNode } from "react";
import { cx } from "../../ui/kit";

/** HeroGreeting: heading (36/44, semibold, ink) whose first line carries an inline badge cluster,
 *  then a 16/22 paragraph in text-2, 16 px below. Drops to 28/36 under 640 px. */
export function HeroGreeting({ greeting, question, body, badges, className, as: H = "h1" }: {
  greeting: string; question: string; body?: ReactNode; badges?: ReactNode; className?: string; as?: "h1" | "h2";
}) {
  return (
    <div className={cx("min-w-0", className)}>
      <H className="text-[28px] leading-9 font-semibold tracking-[-0.01em] text-r12-ink sm:text-[36px] sm:leading-[44px]">
        <span className="inline-flex flex-wrap items-center gap-x-3">{greeting}{badges && <span className="inline-flex items-center">{badges}</span>}</span>
        <span className="block">{question}</span>
      </H>
      {body && <p className="mt-4 max-w-[400px] text-[15px] leading-[22px] text-r12-text-2 sm:text-[16px]">{body}</p>}
    </div>
  );
}

/** HeroBadge: the 28 px round glyph chips after the greeting (reference: two purple people/ring glyphs). */
export function HeroBadge({ children, label }: { children: ReactNode; label: string }) {
  return <span role="img" aria-label={label} title={label} className="-ml-1.5 inline-flex size-7 items-center justify-center rounded-full border border-r12-line bg-r12-card text-r12-accent first:ml-0">{children}</span>;
}
