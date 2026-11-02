import { lazy, StrictMode, Suspense, useEffect } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import { AppProvider, useApp } from "./lib/app";
import { Shell } from "./shell/Shell";
import { ErrorBoundary } from "./shell/ErrorBoundary";
import { store } from "./lib/format";
import Queue from "./pages/Queue";
import Create from "./pages/Create";
import Library from "./pages/Library";
import Assistant from "./pages/Assistant";
import Channels from "./pages/Channels";
import Results from "./pages/Results";
import Settings from "./pages/Settings";

// Design review routes (#/design, #/design/ref12) render full-bleed, outside the app shell, and load on demand.
const DesignSheet = lazy(() => import("./pages/DesignSheet"));
const DesignRef12 = lazy(() => import("./pages/DesignRef12"));
const DESIGN: Record<string, React.LazyExoticComponent<() => JSX.Element>> = { design: DesignSheet, "design-ref12": DesignRef12 };

const PAGES: Record<string, () => JSX.Element | null> = { queue: Queue, create: Create, library: Library, assistant: Assistant, channels: Channels, results: Results, settings: Settings };
const TITLES: Record<string, string> = { queue: "Queue", create: "Create", library: "Library", assistant: "Assistant", channels: "Channels", results: "Results", settings: "Settings" };

function Router() {
  const { route, status } = useApp();
  const Page = PAGES[route.name] || Queue;
  useEffect(() => { const n = status?.outbox?.pending; document.title = `${TITLES[route.name] || "Queue"}${n ? ` (${n})` : ""} · Nightshift`; }, [route.name, status?.outbox?.pending]);
  useEffect(() => { window.scrollTo(0, 0); document.getElementById("main")?.focus({ preventScroll: true }); }, [route.name]);
  if (!status) return <div className="h-[60vh]" aria-busy="true" />;
  // A crash in one page leaves the shell and the other pages alone, and forgets the remembered tab that may have caused it.
  const forget = () => { store.set("ns-lib-tab", "ideas"); store.set("ns-create-kind", "story"); };
  return <ErrorBoundary key={route.name} name={TITLES[route.name] || "This page"} onCrash={forget}><Page /></ErrorBoundary>;
}

function Root() {
  const { route } = useApp();
  const Design = DESIGN[route.name];
  if (Design) return <Suspense fallback={<div className="h-dvh bg-ds-canvas" aria-busy="true" />}><Design key={route.name} /></Suspense>;
  return <Shell><Router /></Shell>;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary name="the app" root>
      <AppProvider>
        <Root />
      </AppProvider>
    </ErrorBoundary>
  </StrictMode>,
);
