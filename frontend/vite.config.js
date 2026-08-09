import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // electron.cjs loads localhost:5174 and `npm run electron` waits on it,
  // so pin the port rather than letting Vite pick the next free one.
  server: {
    port: 5174,
    strictPort: true,
  },
})
