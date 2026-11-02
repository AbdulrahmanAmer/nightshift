import { cx } from "../../ui/kit";
import { Card } from "./Card";
import { ProgressBar } from "./ProgressBar";
import { pad, useNow } from "./useNow";

const dateLine = (d: Date) => d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });

/** DigitalClock (Planify Clocks.tsx). `card`: centred readout in a padding-md card with an optional meter.
 *  `inline`: the top bar size, md/600 time over a 2xs muted date, no card. Live unless `value` is given. */
export function DigitalClock({ value, meter, variant = "card", className }: { value?: Date; meter?: { value: number; label: string }; variant?: "card" | "inline"; className?: string }) {
  const now = useNow(variant === "inline" ? 15_000 : 1000, value);
  const hm = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
  if (variant === "inline") {
    return (
      <p className={cx("flex shrink-0 flex-col items-end leading-ds-tight", className)}>
        <time dateTime={now.toISOString()} className="text-ds-md font-semibold ds-num">{hm}</time>
        <span className="text-ds-2xs text-ds-muted">{dateLine(now)}</span>
      </p>
    );
  }
  return (
    <Card as="section" padding="md" aria-label="Clock" className={cx("flex flex-col items-center text-center", className)}>
      <time dateTime={now.toISOString()} className="text-[2.75rem] leading-none tracking-[0.02em] ds-num">{hm}</time>
      <p className="mt-2 text-ds-md text-ds-text-2">{dateLine(now)}</p>
      {meter && <ProgressBar value={meter.value} label={meter.label} className="mt-4 w-full" />}
    </Card>
  );
}
