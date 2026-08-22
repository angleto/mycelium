/// <reference types="vite/client" />

/**
 * Identity of the bundle currently executing, generated per build by the
 * `mycelium-build-identity` plugin in vite.config.ts. The same value is
 * published beside the bundle as `/version.json`; comparing the two is
 * how the app notices it is no longer the frontend the server serves
 * (see lib/useBuildWatch.ts).
 *
 * A virtual module, not a `define`: Vite's define substitution does not
 * run in dev, which would leave the mechanism working only in production.
 */
declare module 'virtual:mycelium-build-id' {
  export const BUILD_ID: string
}
