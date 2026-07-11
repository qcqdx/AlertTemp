import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// dev-режим: vite на :5173 проксирует API в backend на :8000 —
// cookie-сессии работают без CORS-настроек
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
      '/healthz': 'http://localhost:8000',
    },
  },
  build: {
    chunkSizeWarningLimit: 1200, // echarts — крупный, но единственный тяжёлый чанк
  },
})
