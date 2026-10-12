import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies /api to the control plane rather than enabling CORS on
// it. CORS would have to be configured for production too, where there is no
// cross-origin request to allow — the built bundle is served by the control
// plane itself, from the same origin the bridge's verification URL points at.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
