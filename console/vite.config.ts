/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { createReadStream, statSync } from "node:fs";
import { extname, join, normalize, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Connect, type Plugin } from "vite";

/**
 * Policy enforced by `npm run preview`. It has the same shape as the OperatorConsole response-headers policy
 * (SPEC §6): no inline script or style, blob: for the hls.js worker and MSE, and AWS hosts for connect-src
 * (the SGAI asset lists live on interstitials.mediatailor.<region>, _review.md §6). So a CSP regression shows up
 * locally before it reaches CloudFront.
 */
const PREVIEW_CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  // Cloudscape global-styles embeds Open Sans as data: WOFF2.
  "font-src 'self' data:",
  // hls.js plays through MSE blob: URLs and gives the metadata text track behind its EXT-X-DATERANGE cues (which time
  // HLS Interstitials) a data:,WEBVTT source (texttrack-utils.ts createTrackNode).
  "media-src 'self' blob: data:",
  "worker-src 'self' blob:",
  "connect-src 'self' https://*.amazonaws.com https://*.amazoncognito.com",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

const MEDIA_TYPES: Record<string, string> = {
  ".m3u8": "application/vnd.apple.mpegurl",
  ".ts": "video/mp2t",
  ".json": "application/json",
  ".gif": "image/gif",
};

/** Serves the mock-mode media fixtures (`mock/cdn`) at `/mock/cdn` in dev and preview, never in the build output. */
function mockCdn(): Plugin {
  const root = fileURLToPath(new URL("./mock/cdn", import.meta.url));
  const serve: Connect.NextHandleFunction = (req, res, next) => {
    const path = normalize(join(root, decodeURIComponent(new URL(req.url ?? "/", "http://mock").pathname)));
    if (!path.startsWith(root + sep) || !statSync(path, { throwIfNoEntry: false })?.isFile()) {
      next();
      return;
    }
    res.setHeader("Content-Type", MEDIA_TYPES[extname(path)] ?? "application/octet-stream");
    res.setHeader("Cache-Control", "no-store");
    createReadStream(path).pipe(res);
  };
  return {
    name: "fast-mock-cdn",
    configureServer: (server) => void server.middlewares.use("/mock/cdn", serve),
    configurePreviewServer: (server) => void server.middlewares.use("/mock/cdn", serve),
  };
}

export default defineConfig({
  plugins: [react(), mockCdn()],
  build: {
    target: "es2023",
    // Emit every asset as a file: data: URIs would need a looser font-src/img-src.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    chunkSizeWarningLimit: 1500,
  },
  preview: { headers: { "Content-Security-Policy": PREVIEW_CSP } },
  test: { include: ["src/**/*.test.ts"] },
});
