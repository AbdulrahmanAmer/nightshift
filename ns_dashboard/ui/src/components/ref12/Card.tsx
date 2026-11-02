import { forwardRef, type ElementType, type HTMLAttributes } from "react";
import { cx } from "../../ui/kit";

export type Elevation = 0 | 1 | 2 | 3;
const E: Record<Elevation, string> = { 0: "shadow-ds-0", 1: "shadow-ds-1", 2: "shadow-ds-2", 3: "shadow-ds-3 z-10" };
const P = { none: "p-0", sm: "px-4 py-3", md: "px-5 py-4", lg: "p-6" } as const;
const R = { lg: "rounded-ds-lg", xl: "rounded-ds-xl", "2xl": "rounded-ds-2xl" } as const;
const TONE = { surface: "bg-ds-surface text-ds-text", sunken: "bg-ds-sunken text-ds-text", accent: "bg-ds-accent text-ds-on-accent" } as const;

/** Card (Planify): the base surface. elevation 0 flat · 1 resting · 2 raised (feature tiles, active row) ·
 *  3 floating (swiped notification). padding sm 12/16 · md 16/20 · lg 24. radius lg 16 · xl 24 · 2xl 32.
 *  interactive = lifts 2 px and steps to elevation 2 on hover. */
export type CardProps = HTMLAttributes<HTMLElement> & {
  as?: ElementType; elevation?: Elevation; padding?: keyof typeof P; radius?: keyof typeof R; tone?: keyof typeof TONE; interactive?: boolean;
};
export const Card = forwardRef<HTMLElement, CardProps>(function Card({ as: Tag = "div", elevation = 1, padding = "lg", radius = "xl", tone = "surface", interactive, className, ...rest }, ref) {
  return (
    <Tag ref={ref} {...rest} className={cx(
      "relative min-w-0 transition-[box-shadow,transform] duration-200 ease-ds-out",
      TONE[tone], tone === "accent" && elevation === 2 ? "shadow-ds-accent" : E[elevation], P[padding], R[radius],
      interactive && "ds-focus cursor-pointer hover:-translate-y-0.5 hover:shadow-ds-2",
      className,
    )} />
  );
});
