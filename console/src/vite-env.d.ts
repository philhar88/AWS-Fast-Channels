/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** `1` runs the console against in-browser fixtures instead of a deployed backend. */
  readonly VITE_MOCK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
