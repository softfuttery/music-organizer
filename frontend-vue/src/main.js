import { createApp } from 'vue'
import App from './AppModern.vue'
import './modern.css'
import './design-system.css'
import './theme.css'
import './admin-workspaces.css'
import { applyTheme, loadThemePreference } from './theme-preferences'

applyTheme(loadThemePreference())

createApp(App).mount('#app')

if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    const cacheVersion = encodeURIComponent(import.meta.env.VITE_SHELL_CACHE_VERSION)
    navigator.serviceWorker.register(`/sw.js?v=${cacheVersion}`).catch(() => {})
  })
}
