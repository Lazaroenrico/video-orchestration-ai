import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The FastAPI backend runs on :8000 (see `orchestrator serve`). In dev we proxy the
// JSON/SSE/media routes there so the SPA can talk to the real orchestrator. In prod
// `npm run build` emits ./dist, which FastAPI serves at "/".
const backend = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/media": { target: backend, changeOrigin: true },
      "/videos": { target: backend, changeOrigin: true },
    },
  },
});
