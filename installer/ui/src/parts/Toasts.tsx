// Toasts, the connection banner and the one aria-live region (docs/design/installer-ui.md section 3.5).
import { Card } from "@ref12";
import { shell } from "../copy";
import type { ToastItem } from "../api/useSetup";
import { SetupIcon } from "./icons";

/** Visible toasts. They are not live regions themselves: the text is announced once, through <LiveRegion>. */
export function Toasts({ items }: { items: ToastItem[] }) {
  return (
    <div className="pointer-events-none fixed bottom-[76px] left-1/2 z-50 flex -translate-x-1/2 flex-col items-center gap-2">
      {items.map((t) => (
        <Card key={t.id} elevation={3} radius="lg" padding="sm" className="ns-toast pointer-events-auto text-ds-sm">{t.text}</Card>
      ))}
    </div>
  );
}

export function ConnectionBanner({ show }: { show: boolean }) {
  if (!show) return null;
  return (
    <div className="pointer-events-none fixed top-2 left-1/2 z-40 -translate-x-1/2">
      <Card elevation={3} radius="lg" padding="sm" className="ns-toast flex items-center gap-3 text-ds-sm">
        <SetupIcon name="offline" size={16} className="shrink-0 text-ds-text-2" />
        <span>{shell.connectionLost}</span>
      </Card>
    </div>
  );
}

/** The single polite live region: phase changes, failures, toasts, and download progress every 30 s. */
export function LiveRegion({ text }: { text: string }) {
  return <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">{text}</div>;
}
