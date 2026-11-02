import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./main.css";
import { App } from "./App";

// ?theme=light|dark picks the theme before the first paint (the installer defaults to dark; Choices saves the owner's pick).
const params = new URLSearchParams(window.location.search);
const theme = params.get("theme");
if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;

// Demo only: ?host=1 fakes the WebView2 bridge so the window notches can be looked at in a normal browser.
if (params.has("host") && !window.chrome?.webview) {
  (window as unknown as { chrome: unknown }).chrome = { webview: { postMessage: () => undefined, addEventListener: () => undefined, removeEventListener: () => undefined } };
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
