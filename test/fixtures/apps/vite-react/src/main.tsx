import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import posthog from 'posthog-js'
import App from './App'

posthog.init(import.meta.env.VITE_POSTHOG_KEY, {
  api_host: 'https://eu.i.posthog.com',
  person_profiles: 'identified_only',
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
