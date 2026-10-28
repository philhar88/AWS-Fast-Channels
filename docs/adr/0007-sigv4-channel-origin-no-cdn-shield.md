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
MediaTailor SSAI reuses Channel Assembly responses across sessions for up to the origin's `max-age`, so Channel Assembly
sees far fewer requests than players make. In a load test of one channel up to 1,600 concurrent sessions, it leveled off
near 9 requests per second. The per-channel quota therefore does not bound the audience. MediaTailor's account-wide Ad
Insertion requests quota does (see [capacity](../delivery-and-multi-cdn.md#capacity)). A CDN shield would add little
offload. A CDN-shielded mode can return as an opt-in once MediaTailor supports OAC.
