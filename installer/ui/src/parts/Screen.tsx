// Layout shared by every screen (docs/design/installer-ui.md section 2.0): header (Greeting), a body that is a 6 column grid
// (4 columns under 1020 px of content width, then the body scrolls), and a footer that stays at the bottom of the frame.
import type { ReactNode } from "react";
import { Greeting } from "@ref12";
import { cx } from "@dash/ui/kit";

export function Screen({ name, salutation, question = "", description, headerRight, footerLeft, footerRight, children }: {
  name: string; salutation: string; question?: string; description?: ReactNode; headerRight?: ReactNode; footerLeft?: ReactNode; footerRight?: ReactNode; children: ReactNode;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col pt-10 pr-8 pb-4 pl-[112px]">
      <header className="flex shrink-0 items-start justify-between gap-6 pb-4">
        <Greeting as="h1" size="lg" salutation={salutation} question={question} description={description} className="[&_p]:mt-2 [&_p]:max-w-[72ch]" />
        {headerRight && <div className="shrink-0 pt-1">{headerRight}</div>}
      </header>
      <div id="main" tabIndex={-1} className="ns-scroll scroll-thin -mx-2 min-h-0 flex-1 overflow-x-hidden overflow-y-auto px-2 pt-1 pb-3 outline-none @container">
        <div className={cx("ns-grid grid grid-cols-4 gap-5 @min-[1020px]:grid-cols-6", `ns-${name}`)}>{children}</div>
      </div>
      <footer className="relative mt-1 flex min-h-10 shrink-0 items-center justify-between gap-4">
        <div className="min-w-0 text-ds-xs text-ds-muted ds-num">{footerLeft}</div>
        <div className="flex shrink-0 items-center gap-3">{footerRight}</div>
      </footer>
    </div>
  );
}

/** A grid cell: places its child in a named grid area (the areas are declared per screen in main.css). */
export function Area({ area, className, fit, children }: { area: string; className?: string; fit?: boolean; children: ReactNode }) {
  return <div style={{ gridArea: area }} className={cx("flex min-h-0 min-w-0 flex-col [&>*]:min-h-0", fit ? "[&>*]:flex-none" : "[&>*]:flex-1", className)}>{children}</div>;
}
