/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Base URL for the API. Empty string means "same origin", which is what both
   * the Vite dev proxy and the nginx container rely on. Inlined at build time.
   */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
