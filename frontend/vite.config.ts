/// <reference types="vitest/config" />
import { fileURLToPath, URL } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Browser dev mode talks to a separately started backend through this proxy.
// The bearer token (if the backend was started with one) is injected here, on
// the server side, so it never reaches browser JavaScript.
const backendUrl = process.env.LOCPILOT_BACKEND_URL ?? 'http://127.0.0.1:8420'
const devToken = process.env.LOCPILOT_DEV_TOKEN

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  clearScreen: false,
  envPrefix: ['VITE_', 'TAURI_ENV_'],
  server: {
    host: '127.0.0.1',
    port: 5420,
    strictPort: true,
    proxy: {
      '/api': {
        target: backendUrl,
        changeOrigin: false,
        configure: (proxy) => {
          proxy.on('proxyReq', (proxyReq) => {
            if (devToken) proxyReq.setHeader('Authorization', `Bearer ${devToken}`)
          })
        },
      },
    },
    watch: { ignored: ['**/src-tauri/**'] },
  },
  worker: { format: 'es' },
  build: {
    target: ['es2022', 'safari16', 'chrome110'],
    chunkSizeWarningLimit: 8000,
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: false,
    restoreMocks: true,
  },
})
