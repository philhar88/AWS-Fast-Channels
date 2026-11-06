# 0018 — Viewer identity through an opaque single-use playback ticket, bound at session init

**Status:** Accepted (2026-11-05). The session leg passed end to end on a throwaway stage: a signed-in viewer's
session carried their `vid` into the Pod Decision Service's decision record, bound by the PRE_SESSION redeem; a
replayed ticket, a ticket opened too late and a guest were CONTEXTUAL (`tests/e2e/test_19_identity_session.py`).

## Context

The Pod Decision Service (PDS, [ADR-0036](0036-pds-sidecar-in-the-prebid-server-task.md)) decides each break's
policy and pod. Without identity it serves every session CONTEXTUAL: no viewer id, no frequency or ad-load history,
no per-viewer consent. To do better it must know who is watching, but MediaTailor sessions are opened by a URL the
viewer controls:

- Any `ads.*` query parameter of the init request reaches the ad request, and `aws.*` parameters change how the
  session behaves (`aws.logMode=DEBUG`, `aws.availSuppressionMode`).
- A token or viewer id in the URL lands in CDN logs, player logs, referrers and MediaTailor's session events.
- MediaTailor Functions at `PRE_SESSION_INITIALIZATION` can call one HTTP service per session, with no credential of
  the viewer's to forward.
- A playback URL can be shared, so whatever it carries can be replayed by another viewer.

Live checks of each step of this design (October 2026) found:

- MediaTailor's 2,000-character query limit applies to `manifest.*` parameters only; a 22-character `ads.t` reaches
  the redeem body and the ad request intact.
- CloudFront's standard log records the **original** viewer query even after a viewer-request Function rewrites it.
- A CloudFront Function that throws answers **503** and forwards nothing: a broken edge Function is an outage for
  that viewer, never an anonymous session.
- `session.uuid` at `PRE_SESSION_INITIALIZATION` is the session id the ad request carries later.
- A conditional DynamoDB update binds a fresh ticket to exactly one session; the same ticket from another session
  8 s later was refused, and the PDS then served that session CONTEXTUAL on every break.
- The PRE_SESSION redeem costs p50 117 ms and p99 217 ms of the session's hook budget.

## Decision

Identity reaches the decision as a row, never as a value in a URL or a session variable.

1. **Mint.** The viewer app signs in to the stage's Cognito user pool and sends its **access token** to
   `POST /playback` with a channel name. The method's authorizer requires the scope `fast/playback`, which the pool's
   pre-token-generation trigger adds to access tokens; ID tokens carry no scope and are refused. The mint draws an
   opaque ticket `t` (`secrets.token_urlsafe(16)`, 22 characters, 128 bits) and writes the row `T#<t>`
   `{pc, pm, cid, iat, redeem_by, ttl, vid, persona}`, where `pc` is the channel's stable sibling configuration and
   `redeem_by` is 120 s after the mint. It answers the stable sibling's playback URL with `?ads.t=<t>`.
2. **`vid`, not `sub`.** The viewer id is `HMAC-SHA256(k, iss | sub)` truncated to 128 bits, computed by KMS with a
   per-stage HMAC key that never leaves it. The raw `sub` is never written to the row, a log or the lake, and the
   `vid` is never sent to the player.
3. **Edge rebuild.** A CloudFront Function on the playback paths first sets `querystring = {}`, then copies back
   `ads.t` only when it is single-valued and matches `^[A-Za-z0-9_-]{22}$`. Every other viewer parameter is dropped.
   It refuses `/v1/session` (403) and non-canonical init paths (400), and the behaviours allow only GET, HEAD and
   OPTIONS. It catches every error, because a throw is a 503. The CDN's standard log leaves out `cs-uri-query`.
4. **Bind at init.** The sibling's `PRE_SESSION_INITIALIZATION` Function posts `{t, sid: session.uuid, pc}` to
   `POST /redeem`, an API Gateway AWS integration that runs one conditional `UpdateItem` with no Lambda:
   `attribute_exists(pk) AND pc = :pc AND ((attribute_not_exists(redeemedBy) AND redeem_by > :now) OR redeemedBy = :sid)`.
   A retried hook of the same session succeeds again; any other session gets a 409 and stays anonymous. The request
   is cache scope `none` ([ADR-0034](0034-every-function-request-cached-by-scope.md)): it is single-use.
5. **The PDS is the authority.** On every break the PDS reads `T#<t>` and treats the viewer as signed in only if
   `redeemedBy` equals the request's session id and `pc` its configuration, inside the binding's lease. Otherwise the
   break is CONTEXTUAL, with the reason (`missing`, `replay`, `pc_mismatch`, `expired_or_raced`) in its decision
   record. A missing, malformed, expired or replayed ticket is never an error: the session plays with contextual ads.
6. **No identity in session variables.** Nothing the Functions write to the session carries the `vid`, the persona
   or a tier; the PDS resolves them from the row and its own viewer profiles.

## Consequences

- A viewer value reaches MediaTailor only as 22 random characters that are useless after their first session.
  Tokens stay between the app and the mint.
- The ticket is in CloudFront's request, so the standard log omits `cs-uri-query` (and the viewer's address), and
  the opt-in MediaTailor events that carry it (`PRE_SESSION_INIT_FUNCTION_COMPLETED`, `RAW_ADS_REQUEST`) stay off:
  the lab channel's siblings, which publish them, are never ticketed.
- `POST /redeem` takes no credential, because the MediaTailor Function has none to send. Guessing a live ticket means
  guessing 128 random bits inside its 120 s window, under the API stage's throttling.
- Every play costs one mint (a Lambda, one KMS `GenerateMac` and one DynamoDB write) and one redeem in the session's
  hook budget.
- **A failed PRE_SESSION redeem costs nothing but the binding.** The redeem writes only a temporary, so a 409 or a
  timeout leaves the session as it was, and the PDS binds a fresh ticket itself inside its redeem window.
- **Residual risk: a direct caller.** MediaTailor's endpoints are public and honour player-set `aws.*` at init, and a
  failed PRE_SESSION hook keeps them: a caller who skips the CDN and makes the hook fail gets an ad-free session
  (W-2 FR-1). The edge rebuild closes the CDN path; the `PreSessionInitHook.Errors` alarms and the zero-ad-sessions
  detector of the `ops` module detect the rest. Both are marked `OPTIMIZATION(aws): W-2 FR-1`
  ([Workarounds to remove](../workarounds.md)).
- A lookup on every break adds one consistent DynamoDB read to `/policy`'s budget; a failed read makes the break SAFE
  rather than CONTEXTUAL, because the session may be a signed-in viewer's.
- The viewer app must fall back to the plan's anonymous URL when the mint fails, and must ask for a new ticket for
  every play, retry and channel change.

## Alternatives considered

- **A signed JWT or viewer id in the playback URL.** It reaches every log and referrer, can be replayed for its whole
  lifetime, and gives the viewer a value to edit that the ad request trusts.
- **Session variables for the viewer's tier and consent.** They would be set from a viewer-controlled request and
  read by every later Function and ad request; the PDS would have to trust them.
- **A Lambda behind the redeem.** It adds a cold start to the session's hook budget for what is one conditional
  write.
- **Lambda@Edge for the rebuild.** A CloudFront Function does the rebuild in under a millisecond, at a fraction of the
  cost, and can be tested before publishing with `TestFunction`.
