import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      manifest: {
        name: "Stock Profiler",
        short_name: "Stock Profiler",
        display: "standalone",
        theme_color: "#14532d",
        background_color: "#f8fafc"
      },
      workbox: {
        runtimeCaching: [],
        navigateFallbackDenylist: [/^\/api\//]
      }
    })
  ],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000"
    }
  }
});
