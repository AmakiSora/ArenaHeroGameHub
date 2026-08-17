import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Served by ArenaGame's dashboard.py under /arena (static files + API proxy).
  base: '/arena/',
  server: {
    port: 3000,
    proxy: {
      // Local dashboard: it injects the game API key and forwards upstream.
      '/api': { target: 'http://localhost:4399', changeOrigin: true, ws: true },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
  },
})
