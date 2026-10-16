import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { port: 5520, strictPort: true, host: true },
  preview: { port: 5520, strictPort: true },
  build: { chunkSizeWarningLimit: 4000 },
});
