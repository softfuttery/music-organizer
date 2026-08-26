import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const shellCacheVersion = createHash('sha256')
  .update(readFileSync(new URL('./public/sw.js', import.meta.url)))
  .update(readFileSync(new URL('./public/manifest.webmanifest', import.meta.url)))
  .update(readFileSync(new URL('./public/app-icon.svg', import.meta.url)))
  .digest('hex')
  .slice(0, 12)

export default defineConfig({
  plugins: [vue()],
  define: {
    'import.meta.env.VITE_SHELL_CACHE_VERSION': JSON.stringify(shellCacheVersion),
  },
  // Keep mapped Windows drive paths (for example Z:) stable instead of
  // resolving part of the module graph to the underlying UNC share.
  resolve: {
    preserveSymlinks: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_BACKEND_URL || 'http://127.0.0.1:15000',
        changeOrigin: true,
      },
    },
  },
})
