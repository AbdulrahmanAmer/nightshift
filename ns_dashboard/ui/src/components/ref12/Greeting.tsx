import { useId, type ReactNode } from "react";
import { cx } from "../../ui/kit";

/** Greeting (Planify styling, generic copy): display/600 heading at tight leading and -0.02em, balanced wrap;
 *  the salutation line carries an inline adornment (WorkspaceTokens) 12 px after it; md secondary paragraph,
 *  max 34ch, 20 px below. Drops to 2xl under 640 px. size "lg" (32 px) is for longer salutations in narrow columns. */
export function Greeting({ salutation, question, description, adornment, as: H = "h1", size = "display", className }: {
  salutation: string; question: string; description?: ReactNode; adornment?: ReactNode; as?: "h1" | "h2"; size?: "display" | "lg"; className?: string;
}) {
  const id = useId();
  return (
    <section aria-labelledby={id} className={cx("min-w-0", className)}>
      <H id={id} className={cx("flex flex-col text-ds-2xl leading-ds-tight font-semibold tracking-[-0.02em] [text-wrap:balance]", size === "display" ? "sm:text-ds-display" : "sm:text-[2rem]")}>
        <span className="inline-flex flex-wrap items-center gap-3">{salutation}{adornment}</span>
        <span>{question}</span>
      </H>
      {description && <p className="mt-5 max-w-[34ch] text-ds-md leading-ds-normal text-ds-text-2">{description}</p>}
    </section>
  );
}
