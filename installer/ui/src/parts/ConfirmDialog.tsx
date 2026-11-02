// Native <dialog> holding a Card (docs/design/installer-ui.md section 2.9): 440 px wide, modal (the page behind is inert, so
// focus stays inside), Escape cancels. The button marked `initial` gets the focus.
import { useEffect, useRef, type ReactNode } from "react";
import { Button, Card, type ButtonVariant } from "@ref12";

export interface DialogAction { label: string; variant: ButtonVariant; onClick: () => void; initial?: boolean; disabled?: boolean }

export function ConfirmDialog({ title, body, actions, onCancel, children, wide }: { title: string; body?: ReactNode; actions: DialogAction[]; onCancel: () => void; children?: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (!d.open) d.showModal();
    (d.querySelector("[data-initial]") as HTMLElement | null)?.focus();
    return () => { if (d.open) d.close(); };
  }, []);
  return (
    <dialog ref={ref} aria-labelledby="ns-dlg-title" onCancel={(e) => { e.preventDefault(); onCancel(); }} onClick={(e) => { if (e.target === ref.current) onCancel(); }} className="ns-dialog ds-root">
      <Card elevation={3} radius="2xl" padding="lg" className={wide ? "w-[560px] max-w-[calc(100vw-48px)]" : "w-[440px] max-w-[calc(100vw-48px)]"}>
        <h2 id="ns-dlg-title" className="text-ds-xl leading-ds-tight font-semibold tracking-[-0.01em]">{title}</h2>
        {body && <p className="mt-3 text-ds-md leading-ds-normal text-ds-text-2">{body}</p>}
        {children}
        <div className="mt-6 flex flex-wrap justify-end gap-3">
          {actions.map((a) => (
            <Button key={a.label} variant={a.variant} size="lg" disabled={a.disabled} onClick={a.onClick} {...(a.initial ? { "data-initial": "" } : {})}>{a.label}</Button>
          ))}
        </div>
      </Card>
    </dialog>
  );
}
