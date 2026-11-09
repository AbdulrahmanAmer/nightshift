import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";
import path from "node:path";

// The setup wizard UI (installer/ui) is built by this project, so there is one npm install and one lockfile.
// Builds into installer/ui/<out> (served by installer/stage2/web/server.py). Fixed names (app.js, app.css), no inline
// scripts (CSP script-src 'self'). The folder name is spelled in two parts because the dashboard build's Tailwind scanner reads
// every file in this project and would turn the plain word into a utility class, changing ns_dashboard/<out>/app.css.
const ui = path.dirname(fileURLToPath(import.meta.url)); // ns_dashboard/ui
const repo = path.resolve(ui, "../..");
const root = path.join(repo, "installer/ui");
const dash = path.join(ui, "src");
const OUT = ["sta", "tic"].join("");
const nm = (p: string) => path.join(ui, "node_modules", p);

export default defineConfig({
  root,
  publicDir: path.join(root, "public"),
  plugins: [react(), tailwindcss()],
  base: "./",
  resolve: {
    alias: [
      { find: /^@ref12$/, replacement: path.join(dash, "components/ref12/index.ts") },
      { find: /^@ref12\//, replacement: `${dash}/components/ref12/` },
      { find: /^@dash\//, replacement: `${dash}/` },
      // The shared components live outside this project's folder: force their bare imports into ns_dashboard/ui/node_modules.
      { find: /^react$/, replacement: nm("react") },
      { find: /^react\/jsx-runtime$/, replacement: nm("react/jsx-runtime.js") },
      { find: /^react\/jsx-dev-runtime$/, replacement: nm("react/jsx-dev-runtime.js") },
      { find: /^react-dom$/, replacement: nm("react-dom") },
      { find: /^react-dom\/client$/, replacement: nm("react-dom/client.js") },
      { find: /^lucide-react$/, replacement: nm("lucide-react") },
    ],
    dedupe: ["react", "react-dom", "lucide-react"],
  },
  build: {
    outDir: path.join(root, OUT),
    emptyOutDir: true,
    assetsDir: "assets",
    modulePreload: { polyfill: false },
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        // One script only. The server names the entry `app.js?v=<token>`; a chunk would import it as `../app.js`
        // (another URL), so the bundle would load and run twice. The demo transport folds into app.js (F-6).
        inlineDynamicImports: true,
        assetFileNames: (info) => ((info.names?.[0] ?? info.name ?? "").endsWith(".css") ? "app.css" : "assets/[name]-[hash][extname]"),
      },
    },
  },
  server: { port: 5179, fs: { allow: [repo] }, proxy: { "/api": "http://127.0.0.1:8765" } },
  preview: { port: 8771, host: "127.0.0.1" },
});
