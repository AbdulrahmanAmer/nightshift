// "Copy test report" (docs/design/installer-ui.md section 2.6): fetch verify-report.json, write it to the clipboard, toast.
// When the clipboard is refused, a dialog shows the JSON in a read-only textarea with a "Select all" button.
import { useRef, useState } from "react";
import { verify as t } from "../copy";
import { useLiveState } from "../api/useSetup";
import { ConfirmDialog } from "./ConfirmDialog";

export function useCopyReport() {
  const { api, toast } = useLiveState();
  const [text, setText] = useState<string | null>(null);
  const area = useRef<HTMLTextAreaElement>(null);

  const copy = async () => {
    let body: string;
    try { body = await api.fetchText("verify-report.json"); } catch { toast(t.reportCopyFailed); return; }
    try {
      await navigator.clipboard.writeText(body);
      toast(t.reportCopied);
    } catch {
      setText(body);
    }
  };

  const dialog = text == null ? null : (
    <ConfirmDialog title={t.reportDialogTitle} body={t.reportDialogBody} onCancel={() => setText(null)} wide
      actions={[
        { label: t.selectAll, variant: "soft", onClick: () => { area.current?.focus(); area.current?.select(); } },
        { label: t.closeDialog, variant: "primary", onClick: () => setText(null), initial: true },
      ]}>
      <textarea ref={area} readOnly value={text} aria-label={t.reportDialogTitle} className="mt-4 h-56 w-full resize-none rounded-ds-md border border-ds-border bg-ds-sunken p-3 font-mono text-ds-xs text-ds-text outline-none focus-visible:border-ds-focus" />
    </ConfirmDialog>
  );
  return { copy, dialog };
}
