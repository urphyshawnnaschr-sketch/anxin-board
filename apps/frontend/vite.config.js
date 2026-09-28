import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

function localPort(name, fallback) {
  const raw = process.env[name]
  if (!raw) return fallback

  const parsed = Number(raw)
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    throw new Error(`${name} must be an integer port between 1 and 65535`)
  }
  return parsed
}

const backendPort = localPort('ANXINBOARD_DEV_BACKEND_PORT', 8000)
const frontendPort = localPort('ANXINBOARD_DEV_FRONTEND_PORT', 5173)

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: frontendPort,
    strictPort: true,
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${backendPort}`,
        // The backend's local-session guard intentionally binds Origin to the browser
        // Host. Keep the browser-facing Host instead of rewriting it to backendPort.
        changeOrigin: false
      }
    }
  }
})
