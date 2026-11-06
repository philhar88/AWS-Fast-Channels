/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { createReadStream, renameSync, statSync } from "node:fs";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, type Connect, type Plugin } from "vite";

/**
 * Policy enforced by `npm run preview`. It has the same shape as the OperatorConsole response-headers policy
 * (SPEC §6): no inline script or style, blob: for the hls.js worker and MSE, and AWS hosts for connect-src (the
 * Runtime, the Gateway and Cognito, whose hosts vary by deployment). So a CSP regression shows up locally before it
 * reaches CloudFront.
 */
const PREVIEW_CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  // Cloudscape global-styles embeds Open Sans as data: WOFF2.
  "font-src 'self' data:",
  // hls.js plays through MSE blob: URLs. For EXT-X-DATERANGE tags, here the ad markers that Channel Assembly writes and
  // MediaTailor keeps in the SSAI playlists, it adds a hidden metadata text track with a data:,WEBVTT source
  // (texttrack-utils.ts createTrackNode). Without data:, the e2e tests log a CSP violation for every session.
  "media-src 'self' blob: data:",
  "worker-src 'self' blob:",
  "connect-src 'self' https://*.amazonaws.com https://*.amazoncognito.com",
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "object-src 'none'",
].join("; ");

/**
 * Policy enforced by `vite preview --mode viewer`, with the shape of the viewer site's response-headers policy
 * (`fast_channels.infra.constructs.viewer`): the operator's, minus AgentCore, plus the viewer pool's Cognito API and
 * the playback and ad panel APIs (their execute-api hosts vary by deployment). The viewer calls no hosted login page.
 */
const VIEWER_PREVIEW_CSP = PREVIEW_CSP.replace(
  "connect-src 'self' https://*.amazonaws.com https://*.amazoncognito.com",
  "connect-src 'self' https://cognito-idp.us-east-1.amazonaws.com https://*.execute-api.us-east-1.amazonaws.com https://*.cloudfront.net",
);

/** The viewer build's entry, emitted as `index.html` of `dist-viewer` (the viewer site's default root object). */
const VIEWER_ENTRY = "viewer.html";

function viewerIndex(): Plugin {
  let outDir = "";
  return {
    name: "fast-viewer-index",
    apply: "build",
    configResolved(config) {
      outDir = resolve(config.root, config.build.outDir);
    },
    closeBundle() {
      renameSync(join(outDir, VIEWER_ENTRY), join(outDir, "index.html"));
    },
  };
}

const MEDIA_TYPES: Record<string, string> = {
  ".m3u8": "application/vnd.apple.mpegurl",
  ".ts": "video/mp2t",
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

/**
 * Two builds of one package: the operator console (`dist`, the default) and, with `--mode viewer`, the reference
 * viewer app (`dist-viewer`, from viewer.html), which the viewer module deploys to a site of its own.
 */
export default defineConfig(({ mode }) => {
  const viewer = mode === "viewer";
  return {
    plugins: [react(), mockCdn(), ...(viewer ? [viewerIndex()] : [])],
    // The viewer's local backend config for `npm run dev:viewer` is public-viewer/config.json, never the console's.
    publicDir: viewer ? "public-viewer" : "public",
    build: {
      target: "es2023",
      // Emit every asset as a file: data: URIs would need a looser font-src/img-src.
      assetsInlineLimit: 0,
      modulePreload: { polyfill: false },
      chunkSizeWarningLimit: 1500,
      ...(viewer ? { outDir: "dist-viewer", rolldownOptions: { input: VIEWER_ENTRY } } : {}),
    },
    preview: { headers: { "Content-Security-Policy": viewer ? VIEWER_PREVIEW_CSP : PREVIEW_CSP } },
    test: { include: ["src/**/*.test.ts"] },
  };
});
