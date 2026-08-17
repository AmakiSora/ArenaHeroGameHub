// Public assets live under the Vite base path (/arena/ when served by the
// dashboard, / in dev and tests). Always build URLs through this helper so a
// base change never breaks sprites.
export function publicAsset(path: string) {
  return `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`
}
