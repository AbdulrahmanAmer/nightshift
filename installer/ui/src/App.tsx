// The shell (docs/design/installer-ui.md section 2.0) and the screen switch on (phase, run, failure, uiStep).
// Layers, bottom to top: canvas, AppWindow frame, step rail, window notches, toasts and dialogs.
import { useCallback, useEffect, useRef, useState } from "react";
import { AppWindow } from "@ref12";
import { SetupContext, demoScreen, useSetupProvider, useSetup } from "./api/useSetup";
import type { Phase, SetupState, UiStep } from "./api/types";
import { closeDialog, phaseLabel, shell, stopDialog } from "./copy";
import { host } from "./host";
import { ConfirmDialog } from "./parts/ConfirmDialog";
import { SetupBackdrop } from "./parts/SetupBackdrop";
import { StepRail, type RailStep } from "./parts/StepRail";
import { ConnectionBanner, LiveRegion, Toasts } from "./parts/Toasts";
import { WindowNotches } from "./parts/WindowNotches";
import { Welcome } from "./screens/Welcome";
import { Checks } from "./screens/Checks";
import { Choices } from "./screens/Choices";
import { Download } from "./screens/Download";
import { Installing } from "./screens/Installing";
import { Verify } from "./screens/Verify";
import { Done } from "./screens/Done";
import { Failed } from "./screens/Failed";
import { Uninstall } from "./screens/Uninstall";

type ScreenId = "welcome" | "welcome-resume" | "checks" | "choices" | "downloading" | "installing" | "verifying" | "done" | "failed";

/** Which screen shows for a state. A stopped or restarted install shows the Welcome resume variant until Continue. */
export function pickScreen(s: SetupState): ScreenId {
  if (s.phase === "failed") return "failed";
  const running = s.phase === "downloading" || s.phase === "installing" || s.phase === "verifying";
  if (running && (s.run === "idle" || s.run === "stopped") && (s.resumed || s.run === "stopped")) return s.blocking && s.run === "idle" ? "checks" : "welcome-resume";
  if (s.phase === "checks") return s.uiStep === "checks" ? "checks" : "welcome";
  return s.phase;
}

const RAIL: { id: string; key: keyof typeof shell.railSteps; icon: RailStep["icon"] }[] = [
  { id: "welcome", key: "welcome", icon: "moon" },
  { id: "checks", key: "checks", icon: "cpu" },
  { id: "choices", key: "choices", icon: "edit" },
  { id: "downloading", key: "downloading", icon: "download" },
  { id: "installing", key: "installing", icon: "archive" },
  { id: "verifying", key: "verifying", icon: "check" },
  { id: "done", key: "done", icon: "film" },
];
const RAIL_AT: Record<ScreenId, number> = { welcome: 0, "welcome-resume": 0, checks: 1, choices: 2, downloading: 3, installing: 4, verifying: 5, done: 6, failed: 3 };
const PHASE_STEP: Record<string, number> = { downloading: 3, installing: 4, verifying: 5 };

function railFor(s: SetupState, screen: ScreenId, setUiStep: (u: UiStep) => Promise<unknown>): RailStep[] {
  let at = RAIL_AT[screen];
  if (screen === "failed") {
    const ph = s.steps.find((x) => x.id === s.failure?.step)?.phase ?? "downloading";
    at = PHASE_STEP[ph] ?? 3;
  }
  const beforeStart = s.uiStep !== null && (s.run === "idle" || s.run === "stopped") && !s.resumed;
  return RAIL.map((r, i) => ({
    id: r.id, label: shell.railSteps[r.key], icon: r.icon,
    state: i < at ? "done" : i === at ? (screen === "failed" ? "failed" : "current") : "upcoming",
    onSelect: beforeStart && i < at && i <= 2 ? () => void setUiStep(r.id as UiStep) : undefined,
  }));
}

function labelFor(screen: ScreenId | "uninstall"): string {
  switch (screen) {
    case "welcome": case "welcome-resume": return phaseLabel.welcome;
    case "uninstall": return phaseLabel.uninstall;
    default: return phaseLabel[screen as Phase];
  }
}

function Frame() {
  const setup = useSetup();
  const { state, uninstall, conn, act, announce, toasts, announcement, demo } = setup;
  const [stopDlg, setStopDlg] = useState(demoScreen === "stop-dialog");
  const [closeDlg, setCloseDlg] = useState(demoScreen === "close-dialog");
  const [closing, setClosing] = useState(false);

  const uninstallMode = !!uninstall;
  const screen: ScreenId | null = state ? pickScreen(state) : null;
  const label = uninstallMode ? labelFor("uninstall") : screen ? labelFor(screen) : shell.connecting;

  // Window title and the live announcement follow the phase.
  useEffect(() => { document.title = `${shell.windowTitle} - ${label}`; announce(label); }, [label, announce]);
  const failure = state?.failure;
  useEffect(() => { if (failure) announce(`${label}. ${failure.title}`); }, [failure, label, announce]);

  // Close behaviour (section 6.5).
  const latest = useRef({ state, uninstall, act });
  latest.current = { state, uninstall, act };
  const requestClose = useCallback(async () => {
    const { state: s, uninstall: u, act: a } = latest.current;
    if (u) {
      if (u.running) { host.send({ cmd: "close-ack" }); setCloseDlg(true); return; }
      await a("close-setup"); host.send({ cmd: "close" }); return;
    }
    const run = s?.run;
    if (!run || run === "idle" || run === "stopped" || run === "finished") {
      if (run === "finished" || run === "idle") await a("close-setup");
      host.send({ cmd: "close" });
      return;
    }
    host.send({ cmd: "close-ack" });
    setCloseDlg(true);
  }, []);

  // "Stop setup and close": wait for run = stopped, then close (with a safety timeout).
  useEffect(() => {
    if (!closing) return;
    if (state?.run === "stopped") { host.send({ cmd: "close" }); return; }
    const id = window.setTimeout(() => host.send({ cmd: "close" }), 20000);
    return () => window.clearTimeout(id);
  }, [closing, state?.run]);
  useEffect(() => { if (stopDlg && state && !["running", "paused", "waiting", "pausing"].includes(state.run) && demoScreen !== "stop-dialog") setStopDlg(false); }, [state, stopDlg]);

  // Enter activates the screen's primary button, unless focus is in a field, a button or a dialog.
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.key !== "Enter" || e.defaultPrevented || e.isComposing) return;
      const el = e.target as HTMLElement | null;
      if (el?.closest("input, textarea, select, button, a, summary, dialog, [role=radio], [contenteditable=true]")) return;
      if (document.querySelector("dialog[open]")) return;
      const b = document.querySelector<HTMLButtonElement>("[data-primary]:not(:disabled)");
      if (b) { e.preventDefault(); b.click(); }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  let body: JSX.Element | null = null;
  const onStop = () => setStopDlg(true);
  if (uninstallMode && uninstall) body = <Uninstall view={uninstall} />;
  else if (state && screen) {
    switch (screen) {
      case "welcome": body = <Welcome resume={false} />; break;
      case "welcome-resume": body = <Welcome resume />; break;
      case "checks": body = <Checks />; break;
      case "choices": body = <Choices />; break;
      case "downloading": body = <Download onStop={onStop} />; break;
      case "installing": body = <Installing onStop={onStop} />; break;
      case "verifying": body = <Verify />; break;
      case "done": body = <Done />; break;
      case "failed": body = <Failed onStop={onStop} />; break;
    }
  }

  const rail: RailStep[] = uninstallMode
    ? [{ id: "uninstall", label: shell.railSteps.uninstall, icon: "trash", state: "current" }]
    : state && screen ? railFor(state, screen, setup.setUiStep) : [];
  const stopRunning = state && ["running", "paused", "waiting", "pausing", "stopping"].includes(state.run);

  return (
    <div className="ds-root fixed inset-0 overflow-hidden">
      <SetupBackdrop />
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-16 focus:z-50 focus:rounded-ds-md focus:bg-ds-surface focus:px-4 focus:py-2">{shell.skipToContent}</a>
      <AppWindow className="absolute! inset-4 overflow-hidden">
        {/* key restarts the screen entrance when the screen changes */}
        <div key={uninstallMode ? `u-${uninstall?.result ? "done" : "ask"}` : screen ?? "boot"} className="h-full">
          {body ?? <p className="grid h-full place-items-center text-ds-sm text-ds-text-2">{shell.connecting}</p>}
        </div>
      </AppWindow>
      {rail.length > 0 && <StepRail steps={rail} />}
      <WindowNotches onCloseRequest={requestClose} />
      <ConnectionBanner show={conn === "lost"} />
      <Toasts items={toasts} />
      <LiveRegion text={announcement} />
      {demo && !demoScreen && <p className="pointer-events-none fixed bottom-[3px] left-[120px] z-0 text-[10px] leading-3 text-ds-muted">{shell.demoNote}</p>}

      {stopDlg && (
        <ConfirmDialog title={stopDialog.title} body={stopDialog.body} onCancel={() => setStopDlg(false)}
          actions={[
            { label: stopDialog.keep, variant: "soft", onClick: () => setStopDlg(false), initial: true },
            { label: stopDialog.stop, variant: "primary", onClick: () => { setStopDlg(false); void act("cancel"); } },
          ]} />
      )}
      {closeDlg && (
        <ConfirmDialog title={closeDialog.title} body={uninstallMode ? closeDialog.bodyUninstall : closeDialog.body} onCancel={() => setCloseDlg(false)}
          actions={[
            ...(uninstallMode || !stopRunning ? [] : [{ label: closeDialog.stopAndClose, variant: "ghost" as const, onClick: () => { setCloseDlg(false); setClosing(true); void act("cancel"); } }]),
            { label: closeDialog.keepOpen, variant: "soft", onClick: () => setCloseDlg(false), initial: true },
            { label: closeDialog.close, variant: "primary", onClick: () => { setCloseDlg(false); host.send({ cmd: "close" }); } },
          ]} />
      )}
    </div>
  );
}

export function App() {
  const setup = useSetupProvider();
  return <SetupContext.Provider value={setup}><Frame /></SetupContext.Provider>;
}
