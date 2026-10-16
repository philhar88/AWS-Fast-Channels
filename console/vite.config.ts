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
  // hls.js plays through MSE blob: URLs and gives its text tracks a data:,WEBVTT source (texttrack-utils.ts).
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
  ".m4s": "video/iso.segment",
  ".mp4": "video/mp4",
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

/**
 * Three subtitle fixes for hls.js 1.7.3, applied to the module the console bundles (README, "hls.js patch"). Without
 * them, captions on a live channel stop for good at the first ad break or discontinuity. Each edit must match
 * exactly once, so another hls.js version fails the build until the fixes are reviewed.
 */
const HLS_SUBTITLE_FIXES: readonly (readonly [string, string])[] = [
  [
    // A fragment that fails to parse (SSAI's blank WebVTT in an IMSC1 rendition) has no cues: count it as buffered.
    // Returning left it loaded but never buffered, and the controller waited on it for the rest of the session.
    "      part,\n      success\n    } = data;\n    if (!success) {\n      return;\n    }\n",
    "      part\n    } = data;\n",
  ],
  [
    // Like WebVTT, defer an IMSC1 fragment until the main stream has its discontinuity's initial PTS (onInitPtsFound
    // parses it then); parseIMSC1 read initPTS.baseTime of undefined and stalled subtitle loading.
    "    const hls = this.hls;\n    parseIMSC1(payload, this.initPTS[frag.cc], cues => {",
    "    const hls = this.hls;\n    if (!this.initPTS[frag.cc]) {\n      this.unparsedVttFrags.push(data);\n      return;\n    }\n" +
      "    parseIMSC1(payload, this.initPTS[frag.cc], cues => {",
  ],
  [
    // An SGAI interstitial borrows the media element (transferMedia). Detaching dropped every track's buffered list,
    // which only new subtitle tracks recreate, so the first fragment after the break was never buffered and loading
    // stalled. Start each list over and forget the subtitle fragments, as for new tracks.
    "    this.tracksBuffered = [];\n    super.onMediaDetaching(event, data);\n",
    "    this.tracksBuffered = this.tracksBuffered.map(() => []);\n" +
      "    this.fragmentTracker.removeFragmentsInRange(0, Number.POSITIVE_INFINITY, PlaylistLevelType.SUBTITLE);\n" +
      "    super.onMediaDetaching(event, data);\n",
  ],
];

function hlsSubtitleFixes(): Plugin {
  return {
    name: "fast-hls-subtitle-fixes",
    enforce: "pre",
    transform(code, id) {
      const [path = id] = id.split("?", 1);
      if (!path.endsWith(`${sep}hls.js${sep}dist${sep}hls.mjs`)) {
        return null;
      }
      let fixed = code;
      for (const [before, after] of HLS_SUBTITLE_FIXES) {
        if (fixed.split(before).length !== 2) {
          this.error(`hls.js does not match a subtitle fix any more (README, "hls.js patch"): ${before.trim()}`);
        }
        fixed = fixed.replace(before, after);
      }
      return { code: fixed, map: null };
    },
  };
}

export default defineConfig({
  plugins: [react(), mockCdn(), hlsSubtitleFixes()],
  // Serve hls.js through the plugin pipeline in dev as well (pre-bundled dependencies skip transforms).
  optimizeDeps: { exclude: ["hls.js"] },
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
