/* A page that throws while rendering must not take the shell down with it. Each page is wrapped in one of these
   (main.tsx), keyed by the route so moving to another page starts clean; the whole app has a last one outside. */
import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props { children: ReactNode; /** Shown in the message: "Library", "the app". */ name: string; /** Runs once when the subtree crashes (reset remembered tabs). */ onCrash?: (error: Error) => void; /** Last-resort level: no router links, reloads the page. */ root?: boolean }
interface State { error: Error | null }

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };
  static getDerivedStateFromError(error: Error): State { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`${this.props.name} crashed`, error, info.componentStack);
    try { this.props.onCrash?.(error); } catch { /* resetting a remembered tab is best effort */ }
  }
  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    const retry = () => (this.props.root ? location.reload() : this.setState({ error: null }));
    return (
      <div role="alert" className="mx-auto my-10 max-w-lg rounded-ds-xl bg-ds-surface p-6 shadow-ds-2 text-ds-text">
        <h2 className="text-ds-xl font-semibold">{this.props.root ? "Nightshift hit a problem" : `${this.props.name} hit a problem`}</h2>
        <p className="mt-2 text-ds-md text-ds-text-2">The rest of Nightshift still works. {this.props.root ? "Reload the page to continue." : "Try again, or open another page from the side bar."}</p>
        <p className="mt-3 break-words font-mono text-ds-2xs text-ds-muted">{error.message}</p>
        <div className="mt-5 flex gap-2">
          <button type="button" onClick={retry} className="ds-focus h-9 rounded-ds-md bg-ds-accent px-4 text-ds-sm font-semibold text-white">{this.props.root ? "Reload" : "Try again"}</button>
          {!this.props.root && <a href="#/" className="ds-focus inline-flex h-9 items-center rounded-ds-md px-4 text-ds-sm font-semibold text-ds-text-2 no-underline hover:bg-ds-hover">Open the Queue</a>}
        </div>
      </div>
    );
  }
}
