// IssueCard (docs/design/installer-ui.md section 2.2): an error or warning with its code, a plain title and a "What to do" line.
// Elevation 2, one fix button at most.
import type { ReactNode } from "react";
import { Badge, Card } from "@ref12";
import { common } from "../copy";
import { SetupIcon } from "./icons";

export function IssueCard({ id, tone, code, title, whatToDo, action, children }: { id?: string; tone: "fail" | "warn" | "info"; code?: string | null; title: string; whatToDo: string; action?: ReactNode; children?: ReactNode }) {
  const fail = tone === "fail";
  return (
    <Card as="section" id={id} elevation={2} padding="md" radius="lg" aria-label={title} className="flex flex-col gap-2 scroll-mt-2">
      <div className="flex items-center gap-2">
        <SetupIcon name={fail ? "error" : "warning"} size={18} className={fail ? "shrink-0 text-ds-danger" : "shrink-0 text-ds-text-2"} />
        {code && <Badge tone={fail ? "danger" : "neutral"} size="sm" className="ds-num">{code}</Badge>}
        <h3 className="min-w-0 text-ds-md leading-ds-snug font-semibold">{title}</h3>
      </div>
      <p className="max-w-[44ch] text-ds-sm leading-ds-normal text-ds-text-2"><span className="sr-only">{common.issueWhatToDo}. </span>{whatToDo}</p>
      {children}
      {action && <div className="pt-1">{action}</div>}
    </Card>
  );
}
