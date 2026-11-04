// "Copy status" (journey 10): one line about the setup so far, for the partner to paste to the owner. The server builds the
// line (screens/Failed, Done and every running screen show the same one) and puts it on the clipboard; the page tries the
// browser clipboard too, because the clipboard of the setup program is not reachable from every window host.
import { Button } from "@ref12";
import { status as t } from "../copy";
import { useLiveState } from "../api/useSetup";

export function CopyStatusButton() {
  const { act, toast, busy } = useLiveState();
  const copy = async () => {
    const r = await act("copy-status");
    if (!r.ok || !r.message) return;
    try { await navigator.clipboard.writeText(r.message); } catch { /* the setup program already put it on the clipboard */ }
    toast(t.copied);
  };
  return <Button variant="ghost" size="lg" leadingIcon="copy" disabled={busy.has("copy-status")} onClick={() => void copy()}>{t.button}</Button>;
}
