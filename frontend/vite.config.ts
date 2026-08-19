import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Pinned, not preferred. The backend's CORS_ORIGINS defaults to
    // http://localhost:5173, so a fallback to 5174 would be blocked by CORS
    // and surface as an opaque "failed to fetch".
    port: 5173,
    strictPort: true,
  },
})
