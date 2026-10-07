import path from 'node:path'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(import.meta.dirname, './src'),
    },
  },
  server: {
    // 開發時 Vite 開在 5173、FastAPI 開在 8000，瀏覽器看是不同來源(CORS)。
    // 讓 Vite 把這幾個路徑的請求轉送到後端，瀏覽器就只看到同一個來源，不用在 FastAPI 那邊另外設定 CORS。
    // 正式環境是同一個 container 直接 serve 靜態檔案，本來就是同源，不需要這段。
    proxy: {
      '/auth': 'http://localhost:8000',
      '/workspaces': 'http://localhost:8000',
      '/documents': 'http://localhost:8000',
    },
  },
})
