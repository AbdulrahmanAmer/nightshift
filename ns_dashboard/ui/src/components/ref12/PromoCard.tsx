import { useId, type ReactNode } from "react";
import { cx } from "../../ui/kit";
import { Button } from "./Button";
import { Card } from "./Card";

/** PromoCard (Planify): accent card, radius 2xl, elevation 2 (accent glow), padding 24/20, centred column.
 *  Illustration max 120 px (paper white, navy ink); title xl→2xl/600 one line; xs body at 88%; inverse lg CTA
 *  pinned to the bottom, kept navy in both themes so it reads on the accent. */
export function PromoCard({ illustration, title, description, ctaLabel, onCta, className }: { illustration: ReactNode; title: string; description: string; ctaLabel: string; onCta?: () => void; className?: string }) {
  const id = useId();
  return (
    <Card as="section" tone="accent" radius="2xl" elevation={2} padding="none" aria-labelledby={id} className={cx("flex flex-col items-center px-5 py-6 text-center", className)}>
      <div aria-hidden className="ds-illus-on-accent w-full max-w-[120px]">{illustration}</div>
      <h2 id={id} className="mt-5 text-ds-xl leading-ds-tight font-semibold whitespace-nowrap xl:text-ds-2xl">{title}</h2>
      <p className="mt-3 mb-6 text-ds-xs leading-ds-normal opacity-[0.88]">{description}</p>
      <Button variant="inverse" size="lg" onClick={onCta} className="mt-auto !bg-ds-navy !text-white">{ctaLabel}</Button>
    </Card>
  );
}
