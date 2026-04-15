import path from 'path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Proxy API calls to the backend during `npm run dev` so there are no
    // cross-origin issues when the Vite dev server and the FastAPI backend run
    // on different ports.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8007',
        changeOrigin: true,
      },
    },
  },
  build: {
    // Output directly into backend/static so FastAPI can serve the SPA.
    outDir: path.resolve(__dirname, '../backend/static'),
    emptyOutDir: true,
  },
})
