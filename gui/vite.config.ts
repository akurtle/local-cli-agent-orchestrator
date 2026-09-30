import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build lands inside the Python package, which `agentctl gui` serves.
// In `npm run dev`, API calls are proxied to a running `agentctl gui --port 8765`.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../src/agentos/gui/static",
    emptyOutDir: true,
  },
  server: {
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
});
