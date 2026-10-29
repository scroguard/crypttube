import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // Same-origin in dev too, so the session cookie behaves exactly as behind nginx.
    proxy: { "/api": { target: process.env.API_URL ?? "http://crypttube-api:8000", changeOrigin: false } },
  },
  build: { sourcemap: false, chunkSizeWarningLimit: 800 },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
