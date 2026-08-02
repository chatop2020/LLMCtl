import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  base: '/ui/',
  plugins: [vue()],
  build: {
    outDir: '../lib/account_portal_ui',
    emptyOutDir: true,
    sourcemap: false,
  },
})
