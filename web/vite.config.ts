import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base: './' makes built asset URLs relative, so canvas.py can serve dist/ at '/'.
// dev proxy forwards the JSON API to the Python engine server on :8765.
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8765',
      '/v2': 'http://localhost:8765',
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
