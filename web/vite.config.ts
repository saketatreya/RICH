import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// base: './' keeps built asset URLs relative, so the Python server can serve
// dist/ from '/'.
// dev proxy forwards the JSON API to the Python server on :8767, which also
// serves this app in production.
export default defineConfig({
  plugins: [react()],
  base: './',
  server: {
    port: 5173,
    proxy: {
      '/v1': 'http://127.0.0.1:8767',
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
