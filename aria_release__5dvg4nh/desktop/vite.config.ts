import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

const BACKEND = process.env.ARIA_BACKEND_URL ?? "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],

  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
      "@vendor/ui": path.resolve(__dirname, "./src/components/vendor/nous-ui"),
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 600,
    rollupOptions: {
      output: {
        // P1.2: split stable vendor groups out of the app chunk so the
        // initial main bundle stays well under 500 kB. These groups are
        // cache-friendly and change only on dependency bumps.
        manualChunks(id) {
          // Vendorized copy of the Nous design system lives under src/ and
          // weighs heavily (Command, Toast, Dialog, …) — split it out too.
          // Match on the bare dir name: Vite ids use backslashes on Windows.
          if (id.includes("nous-ui")) return "vendor-nous-ui";
          if (!id.includes("node_modules")) return undefined;
          // P1.2: a single stable vendor chunk (no manual group cycles) keeps
          // the initial app bundle small; xterm is the one heavyweight that
          // deserves its own cacheable chunk (it's only used by ChatPage).
          if (id.includes("xterm")) return "vendor-xterm";
          return "vendor";
        },
      },
    },
  },
  server: {
    port: 1420,
    host: "127.0.0.1",
    strictPort: true,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
    proxy: {
      "/api": { target: BACKEND, ws: true, rewrite: (path) => path.replace(/^\/api/, "") },
    },
  },
});
