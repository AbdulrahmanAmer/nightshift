import { Mail } from "lucide-react";
import { cx } from "../../ui/kit";
import { Kebab } from "./IconButton";
import { ICON } from "./tokens";

/** MessageRow: a notification about a message. 16 px radius white row with the soft row shadow, 16 px padding.
 *  Title 14 semibold with a hairline-divided context ("Message | Product design"), 12 px muted sender line
 *  with a 14 px mail icon, and a 24 px tint preview well with one truncated 11 px line. */
export function MessageRow({ title, context, from, preview, moreLabel, muted, className }: {
  title: string; context?: string; from: string; preview?: string; moreLabel: string; muted?: boolean; className?: string;
}) {
  return (
    <article className={cx("min-w-0 rounded-r12-row bg-r12-card p-4 shadow-r12-row", muted && "opacity-70", className)}>
      <div className="flex items-start justify-between gap-2">
        <h3 className="min-w-0 truncate text-[14px] leading-5 font-semibold text-r12-ink">
          {title}{context && <><span aria-hidden className="mx-1.5 font-normal text-r12-faint">|</span>{context}</>}
        </h3>
        <Kebab label={moreLabel} className="-mt-1 -mr-2" />
      </div>
      <p className="mt-1 flex items-center gap-1.5 text-[12px] leading-4 text-r12-muted"><Mail size={ICON.sm} strokeWidth={ICON.stroke} aria-hidden /><span className="truncate">{from}</span></p>
      {preview && <p className="mt-3 truncate rounded-[6px] bg-r12-tint px-2.5 py-1 text-[11px] leading-4 text-r12-muted">{preview}</p>}
    </article>
  );
}
