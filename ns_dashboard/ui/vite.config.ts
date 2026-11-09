import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Builds into ../static (served by the FastAPI app). Fixed names (app.js, app.css) keep
// tests/test_dashboard_static.py pointed at the real bundle. No inline scripts: CSP is script-src 'self'.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "./",
  build: {
    outDir: "../static",
    emptyOutDir: true,
    assetsDir: "assets",
    modulePreload: { polyfill: false },
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        entryFileNames: "app.js",
        // One script only. The server names the entry `app.js?v=<token>`; a chunk would import it as `../app.js`
        // (another URL), so the bundle would load and run twice. A lazy import folds into app.js (F-6).
        inlineDynamicImports: true,
        assetFileNames: (info) => (info.names?.[0] ?? info.name ?? "").endsWith(".css") ? "app.css" : "assets/[name]-[hash][extname]",
      },
    },
  },
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8100" } },
});
