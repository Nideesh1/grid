import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// /nyiso proxies the public NYISO MIS CSVs (no CORS headers upstream) so the browser can read
// real-time prices for all 11 zones directly.
const nyiso = {
  "/nyiso": { target: "http://mis.nyiso.com", changeOrigin: true, rewrite: (p: string) => p.replace(/^\/nyiso/, "") },
};

export default defineConfig({
  plugins: [react()],
  server: { port: 5520, strictPort: true, host: true, proxy: nyiso },
  preview: { port: 5520, strictPort: true, proxy: nyiso },
  build: { chunkSizeWarningLimit: 4000 },
});
