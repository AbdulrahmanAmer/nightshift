#!/usr/bin/env node
/* Nightshift demo launcher: serves the built dashboard (ns_dashboard/static) on this PC without the Python backend, so
   the owner can click through it as if it were the app. Node 18 or newer, no npm packages.

   Every /api/* request answers 404 at once, which is how lib/api.ts decides there is no engine: the first
   /api/status call fails fast and the UI switches to its built-in demo data for the session.

   Usage: node dev/demo/serve.js [--no-open] [--port 8790] */
"use strict";
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const net = require("node:net");
const { spawn } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..", "..", "ns_dashboard", "static");
const HOST = "127.0.0.1";
const args = process.argv.slice(2);
const noOpen = args.includes("--no-open");
const portArg = args.indexOf("--port");
const FIRST_PORT = portArg >= 0 ? Number(args[portArg + 1]) : 8790;

const MIME = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg",
  ".woff2": "font/woff2", ".woff": "font/woff", ".json": "application/json; charset=utf-8", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
};
// Same policy as the real dashboard (ns_dashboard/server.py), so demo mode proves the page is CSP-clean.
const CSP = "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'";

if (!fs.existsSync(path.join(ROOT, "index.html"))) {
  console.error(`No dashboard build found at ${ROOT}. It ships with the app; run "npm run build" in ns_dashboard/ui if you cloned the source.`);
  process.exit(1);
}

function send(res, status, body, type) {
  res.writeHead(status, { "Content-Type": type, "Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": CSP });
  res.end(body);
}

const server = http.createServer((req, res) => {
  let url;
  try { url = new URL(req.url || "/", `http://${HOST}`); } catch { return send(res, 400, "Bad request", MIME[".txt"]); }
  const p = decodeURIComponent(url.pathname);
  if (p === "/api" || p.startsWith("/api/")) return send(res, 404, JSON.stringify({ error: { message: "Demo mode: no engine" } }), MIME[".json"]);
  if (req.method !== "GET" && req.method !== "HEAD") return send(res, 405, "Method not allowed", MIME[".txt"]);
  const file = path.normalize(path.join(ROOT, p));
  if (!file.startsWith(ROOT)) return send(res, 403, "Forbidden", MIME[".txt"]);
  fs.stat(file, (err, st) => {
    // SPA fallback: anything that is not a real file gets index.html (the app routes by #hash, so this is rare).
    const target = !err && st.isFile() ? file : path.join(ROOT, "index.html");
    fs.readFile(target, (e, data) => {
      if (e) return send(res, 404, "Not found", MIME[".txt"]);
      send(res, 200, req.method === "HEAD" ? "" : data, MIME[path.extname(target).toLowerCase()] || "application/octet-stream");
    });
  });
});

function freePort(port, tries = 50) {
  return new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once("error", () => (tries > 1 ? resolve(freePort(port + 1, tries - 1)) : reject(new Error("No free port found"))));
    probe.once("listening", () => probe.close(() => resolve(port)));
    probe.listen(port, HOST);
  });
}

/* Same order as launchers/nightshift-dashboard.ps1: an app window in Edge, then Chrome, else the default browser. */
function findAppBrowser() {
  if (process.platform !== "win32") return null;
  const leaves = ["Microsoft\Edge\Application\msedge.exe", "Google\Chrome\Application\chrome.exe"];
  const roots = [process.env.ProgramFiles, process.env["ProgramFiles(x86)"], process.env.LOCALAPPDATA].filter(Boolean);
  for (const leaf of leaves) for (const root of roots) { const c = path.join(root, leaf); if (fs.existsSync(c)) return c; }
  return null;
}
function openBrowser(url) {
  const browser = findAppBrowser();
  try {
    if (browser) { spawn(browser, [`--app=${url}`], { detached: true, stdio: "ignore" }).unref(); return; }
    if (process.platform === "win32") spawn("cmd", ["/c", "start", "", url], { detached: true, stdio: "ignore" }).unref();
    else spawn(process.platform === "darwin" ? "open" : "xdg-open", [url], { detached: true, stdio: "ignore" }).unref();
  } catch { console.log("Open the address above in your browser."); }
}

freePort(FIRST_PORT).then((port) => {
  server.listen(port, HOST, () => {
    const url = `http://${HOST}:${port}/`;
    console.log("Nightshift demo is running with sample data (no engine, nothing is changed on this PC).");
    console.log(`Open: ${url}`);
    console.log("Close this window or press Ctrl+C to stop.");
    if (!noOpen) openBrowser(url);
  });
}).catch((e) => { console.error(e.message); process.exit(1); });
