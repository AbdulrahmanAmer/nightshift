import type { ReactNode } from "react";
import { cx } from "../../ui/kit";
import { ActionButton } from "./ActionButton";

/** PremiumCard: the filled accent card. 28 px radius, 24 px padding, centred column, 20 px padding, 12 px gaps:
 *  illustration (96 px tall, navy line art), 20/28 semibold white title, 13/18 white 85% paragraph,
 *  ink button 36 px. The only saturated surface on the screen. */
export function PremiumCard({ title, body, actionLabel, onAction, illustration, className }: {
  title: string; body: string; actionLabel: string; onAction?: () => void; illustration?: ReactNode; className?: string;
}) {
  return (
    <article className={cx("flex min-w-0 flex-col items-center justify-between gap-3 rounded-r12-card bg-r12-accent px-5 py-5 text-center text-white", className)}>
      {illustration && <div aria-hidden className="flex h-24 w-full items-center justify-center [&>svg]:h-full [&>svg]:w-auto">{illustration}</div>}
      <div>
        <h3 className="text-[20px] leading-7 font-semibold">{title}</h3>
        <p className="mt-2 text-[13px] leading-[18px] text-white/85">{body}</p>
      </div>
      <ActionButton variant="ink" onClick={onAction} className="!bg-r12-navy !text-white">{actionLabel}</ActionButton>
    </article>
  );
}
