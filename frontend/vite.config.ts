import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    host: true,
    // The sandbox preview reaches this dev server through a dynamic gateway
    // host (e.g. ws-*.cn-hongkong-vpc.fcapp.run). Dev-only: disable the host
    // check so the preview always loads. Production builds are unaffected.
    allowedHosts: true,
    // Dev-only proxy (D-005): same-origin /api and /ws so the browser only
    // ever talks to this dev server; the backend runs on :8000, the auth
    // mock on :8090 (lets the sandbox preview log in through its own origin).
    proxy: {
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/auth/v1": { target: "http://localhost:8090", changeOrigin: true },
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
  preview: {
    port: 3000,
    host: true,
    allowedHosts: true,
  },
});
