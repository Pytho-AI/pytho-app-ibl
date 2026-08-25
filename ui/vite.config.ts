import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  server: {
    proxy: {
      // geo-agent backend (entities, overlays, maps, custom symbols, etc.).
      // Must precede the generic /api rule so the more specific path wins.
      "/api/v1": {
        target: "http://localhost:9845",
        changeOrigin: true,
        secure: false,
      },
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        secure: false,
      },
      "/files": {
        target: "http://localhost:8000",
        changeOrigin: true,
        secure: false,
      },
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
        changeOrigin: true,
        secure: false,
      },
    },
  },
  plugins: [react()],
  build: {
  // NOTE: Needed when deploying
  chunkSizeWarningLimit: 800,
},
},
)

