# 0028 — A reference viewer on its own origin, with a rule-based rail and a demo-only ad panel

**Status:** Accepted on 2026-11-06: the viewer's end-to-end test (`tests/e2e/test_20_viewer.py`) passed 5 of 5 on
a development stage with the identity, `pds` and `release` modules. Two personas played the same channel on their
own tickets, the panel answered each only for their own session, and every break of the `light` persona had at most
one ad above the bar while the `standard` persona's had two.

## Context

Slice B of v4.1.0 makes per-viewer ad load visible: viewers sign in to the viewer pool (the identity module), each
session plays on a playback ticket of its own (`POST /playback`), and the Pod Decision Service (PDS) gives a
high-value, ad-sensitive viewer fewer, higher-priced ads ([0037](0037-ad-load-by-viewer-tier.md)). The demo needs a
viewer app that shows this: a persona switcher, a rail of recommended channels per persona, a player, and the reason
for each break's ad load.

The repository already has the operator console (`console/`): Vite, React and Cloudscape, an hls.js player with
retry and ad telemetry, the EPG readers, a mock mode and a Playwright harness that runs the production bundle under
its CSP. Every console route sits behind the operator pool's sign-in and the AgentCore backend.

## Decision

- **One package, a second build, its own origin.** The viewer is a second Vite build of `console/`
  (`vite build --mode viewer`, entry `viewer.html`, output `dist-viewer`). It reuses the player, the EPG readers,
  the event log, the mock CDN and the Playwright harness. It deploys to its own private bucket and CloudFront
  distribution in the stack `FastChannels-<prefix>-Viewer` (`modules.viewer`), so viewer and operator tokens never
  share an origin, and its CSP names only what it calls: the media CDN, Cognito's user-pools API, the playback API
  and the ad panel API. The hosting is the console's, factored out as `StaticSite`; the operator templates are
  unchanged.
- **Sign-in with the viewer pool's public client.** The persona switcher signs in with `InitiateAuth`
  `USER_PASSWORD_AUTH` against the identity module's SPA client and sends only the access token, which carries
  `fast/playback`. Tokens live in `sessionStorage`, so two tabs can hold two personas side by side.
- **A ticket per open.** The player asks for a new ticket on every open, reopen and channel change, and aborts a mint
  in flight on close. A guest, an unmonetized channel or a failed mint plays the channel's anonymous URL
  (CONTEXTUAL), never an error; an authorization failure stops the player instead of hiding it.
- **A rule-based rail.** Channels rank by the persona's declared genres against what each channel airs now and next,
  from the public EPG feeds; a channel in an ad break is ranked down. Every tile carries its reasons. No viewer data
  leaves the page and no model is called.
- **A demo-only ad panel.** With `modules.viewer.ad_panel`, an HTTP API with a JWT authorizer reads the PDS's decision
  records of one MediaTailor session with Logs Insights, and answers only when the session's `pds.policy` records
  name the caller's viewer id (computed with the mint's own function). The panel shows the privacy mode, the value
  tier, the ad-load policy and the pod of each break.

## Alternatives considered

- **A route inside the operator console.** Rejected: every route sits behind the operator pool and the AgentCore
  backend, and one origin would hold both pools' tokens.
- **A separate app.** Rejected: it would duplicate the toolchain, the player, its retry and telemetry, the mock CDN
  and the CSP tests.
- **An LLM rail through the model registry.** Deferred. A registry task needs an eval record with the recommender's
  checks (`schema_valid`, `faithfulness`, `policy_violations`, `cost_per_rail`; [0029](0029-per-task-model-registry.md)),
  which needs the recommender's conformance suite and validator (DESIGN §15), and the demo catalog's four titles give
  a model nothing to choose among. The rail's ranking is one pure function, so a recommender can replace it.
- **The ad panel in the operator console**, as DESIGN-engagement §6 draws it (`adload___explain` behind Cedar).
  Deferred: the slice's demo is one window per persona, and the panel answers only for the caller's own session.

## Consequences

- The viewer shows a viewer their value tier while the panel is on. That is a demo affordance: a production viewer
  app must never show a tier, a bar or a bid, because any script on the page could read them. The panel is off by
  default and named as demo-only in the app.
- Each panel refresh runs one Logs Insights query over the PDS sidecar's log group (US$0.005 per GB scanned); the
  function keeps a session's records 15 s, and its API and concurrency are throttled.
- The rail is the genre baseline, not a measured recommender; the demo labels it as such.
