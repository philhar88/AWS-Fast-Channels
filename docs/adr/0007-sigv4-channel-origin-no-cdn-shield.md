# 0007 — SigV4-only Channel Assembly origin; no CDN-shielded mode in v3.0

**Status:** Accepted

## Context
Channel Assembly output should only reach viewers through MediaTailor ad insertion. A channel policy that admits only the
MediaTailor service principal, conditioned on the channel's playback configuration ARN, returns 403 to anonymous
requests and 200 to SSAI (verified). MediaTailor has no CloudFront origin access control type yet. Putting CloudFront
between MediaTailor and Channel Assembly ("CDN-shielded") reduces origin load but needs a public channel policy.

## Decision
SigV4-direct is the only mode in v3.0. CloudFront fronts the MediaTailor manifest and segment origins; the distribution
has an `origin_access_control_id` slot for MediaTailor OAC when it ships. An alarm fires at 80 % of the default 50
manifest requests per second per channel; scaling beyond that is a Service Quotas increase.

## Consequences
SSAI requests reach Channel Assembly roughly 1:1. Large audiences need the quota raised before launch (documented in
operations.md). A CDN-shielded mode can return as an opt-in once MediaTailor supports OAC.
