import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// API_PORT: port the backend runs on (default 8000). UI_PORT: port for this dev server (default 5173).
const apiPort = process.env.API_PORT || '8000'
export default defineConfig({
  plugins: [react()],
  server: { port: Number(process.env.UI_PORT || 5173), proxy: { '/api': `http://localhost:${apiPort}` } },
})
