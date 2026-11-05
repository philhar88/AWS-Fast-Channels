# Viewer identity (`modules.identity`)

The identity module lets a signed-in viewer's play carry who they are to the ad decision, while nothing the viewer
can change reaches MediaTailor. The player asks for a single-use **playback ticket**, and the ticket is the only
viewer value in the playback URL. No token, viewer id, tier or consent value ever travels in a URL. The design is
[ADR-0018](adr/0018-opaque-playback-ticket.md).

The module is off by default. It needs `modules.release`, because a ticket is minted for a monetized channel's stable
sibling configuration. Turn it on with:

```json
"modules": {"identity": {"enabled": true}}
```

`make deploy` then adds the stack `FastChannels-<prefix>-Identity` and puts the media distribution's MediaTailor
route behind the edge rebuild. `make deploy-demand` makes the monetized channels' pair siblings ticketed and gives the
Pod Decision Service's sidecar the ticket lookup.

A play goes like this:

1. **Mint.** The viewer app sends the viewer's access token to `POST /playback` and gets the stable sibling's
   playback URL with a 22-character ticket, `?ads.t=<t>` ([below](#post-playback)).
2. **Edge rebuild.** The CDN's viewer-request function empties the init request's query and puts back only that
   ticket ([below](#the-edge-rebuild)).
3. **Bind at init.** The sibling's PRE_SESSION Function redeems the ticket for its session
   ([below](#the-pre_session-redeem)).
4. **Decide per break.** The Pod Decision Service reads the ticket's row on every break and treats the session as the
   viewer's only if the row is bound to that session and configuration ([below](#the-pod-decision-service)).

A guest, a viewer whose mint failed, a replayed or late ticket all play the same channel with contextual ads.

## What it deploys

| Resource | Name | Purpose |
|---|---|---|
| Cognito user pool (Essentials) | `<prefix>-viewers` | The stage's viewers. Administrators create them; there is no self sign-up, and no email is sent |
| Resource server and scope | `fast`, `fast/playback` | The scope `POST /playback` requires |
| App client | `UserPoolClientId` output | Public, no secret, SRP and password sign-in. It may read `custom:persona` |
| Pre-token-generation trigger (v2) | | Adds `fast/playback` and the claim `fast:persona` to every access token |
| DynamoDB table | `<prefix>-identity` | One row `T#<t>` per ticket, deleted by TTL a day after the longest binding |
| KMS HMAC key | `alias/<prefix>-identity-vid` | The viewer id `vid` |
| REST API, stage `v1` | `<prefix>-identity` | `POST /playback` (the mint) and `POST /redeem` (the binding) |
| SSM parameters | `/<prefix>/identity/<name>` | `user-pool-id`, `client-id`, `e2e-client-id`, `issuer`, `vid-key`, `table`, `playback-url`, `redeem-url` |
| CloudFront Function (Media stack) | `<prefix>-identity-edge` | The edge rebuild on the `/v1/*` behaviour, which then allows GET, HEAD and OPTIONS only |
| Alarm (Media stack, or Edge outside us-east-1) | `<prefix>-identity-edge-errors` | Any `FunctionExecutionErrors` of the edge rebuild |
| Release bundle | `<p>_<sibling>_ps_redeem` | The pair siblings' PRE_SESSION redeem, and their `X-Fast-T` header |
| Sidecar grants | | `GetItem` and `UpdateItem` on the `T#` items of `<prefix>-identity`, through the DynamoDB gateway endpoint |

## Signing in

The viewer app signs in with Cognito `USER_SRP_AUTH` or `USER_PASSWORD_AUTH` on the app client. It sends the
**access token** to `POST /playback`. The token carries the scope `fast/playback` and the persona claim, which the
pool's pre-token-generation trigger adds; password and SRP sign-in grant no custom scope otherwise.

**ID tokens are refused.** They never carry a scope (and they carry the viewer's email), so the method's Cognito
authorizer, which requires `fast/playback`, answers 401.

## `POST /playback`

```http
POST {PlaybackApiUrl}playback
Authorization: Bearer <access token>
Content-Type: application/json

{"channel": "<monetized channel name>"}
```

**200**

```json
{"url": "https://<cdn>/v1/master/<hash>/<configuration>/hls.m3u8?ads.t=<22 characters>", "cid": "<id>", "expires_in": 120}
```

- The URL is single-use. The player opens it within `expires_in` seconds (`modules.identity.redeem_window_seconds`),
  in one player session. Every new play, retry or channel change asks for a new ticket.
- The URL names the channel's stable sibling, from its playback plan `/plan/playback/<channel>.json`.
- `cid` is the mint's correlation id, also on the ticket row.

**Errors** are `{"error": "<code>", "message": "..."}`:

| Status | `error` | When |
|---|---|---|
| 400 | `invalid_request` | The body is not `{"channel": "<name>"}` |
| 400 | `unknown_channel` | The channel is not one of `modules.release.channels` |
| 401 | | No token, an expired token, or a token without `fast/playback` (an ID token) |
| 403 | `forbidden` | A token that is not an access token |
| 503 | `not_released` | The channel has no playback plan yet: it was never released |

CORS allows any origin, with the headers `Authorization` and `Content-Type`. It is a bearer-token API without cookies.
The gateway's own 4xx and 5xx answers carry the CORS header too.

On any error, and for a guest, the player plays the anonymous URL of the plan (`playback.hls`), whose sessions are
CONTEXTUAL.

## What the mint computes

Everything is computed server-side; the request body names only the channel.

- **`vid`**: `HMAC-SHA256(k, iss | sub)` with the KMS HMAC key, truncated to 128 bits and written as 32 hex characters
  (`fast_channels.core.identity.vid.viewer_id`). The key never leaves KMS. The raw `sub` is not written to the row, a
  log or the lake.
- **Privacy mode** `pm`: `PSEUDONYMOUS` for every signed-in viewer (no consent store yet).
- **Persona**: the access token's `fast:persona` claim, the viewer's `custom:persona`, a demo persona id.
- **Configuration** `pc`: the channel's stable sibling.
- **Ticket** `t`: `secrets.token_urlsafe(16)`, 22 URL-safe characters, redrawn while it contains `--` or `__`.

The row `T#<t>` is `{pc, pm, cid, iat, redeem_by, ttl, vid, persona}`. Times are epoch milliseconds, except `ttl`
(epoch seconds, for DynamoDB). Tiers are not on the row: the Pod Decision Service reads them from its own store.

## `POST /redeem`

MediaTailor's PRE_SESSION Function binds a ticket to its session:

```http
POST {RedeemUrl}
Content-Type: application/json

{"t": "<ticket>", "sid": "<session.uuid>", "pc": "<configuration>"}
```

There is no Lambda. An AWS integration runs DynamoDB `UpdateItem` on `T#<t>` with the condition

```
attribute_exists(pk) AND pc = :pc AND ((attribute_not_exists(redeemedBy) AND redeem_by > :now) OR redeemedBy = :sid)
```

and `SET redeemedBy = :sid, bound_at = if_not_exists(bound_at, :now), last_seen = :now`, where `:now` is API
Gateway's request time.

- **200** `{"ok": true}`: the ticket is bound to the session. The same session may redeem again (a retried hook).
- **409** `{"ok": false}`: a replay (another session), an expired or unknown ticket, or a ticket for another
  configuration. The session stays anonymous.
- **400**: a body that is not `{t, sid, pc}` of the expected shapes. A request validator refuses it before DynamoDB.

The redeem takes no credential: MediaTailor's Function has none to send. Binding a ticket needs the ticket itself
(128 random bits), and the stage's throttling (`api_rate_limit`, `api_burst_limit`) caps any guessing.

## The edge rebuild

The media distribution's `/v1/*` behaviour runs `<prefix>-identity-edge`, a CloudFront Function (viewer request,
`cloudfront-js-2.0`, source `infra/src/fast_channels/infra/constructs/identity/edge_rebuild.js`):

| Request | What reaches MediaTailor |
|---|---|
| `GET /v1/master/<40 hex>/<configuration>/<name>.m3u8` (or `/v1/dash/…/<name>.mpd`) | The path, with only `ads.t` when it is one value of 22 URL-safe characters. Every other parameter (`ads.*`, `aws.*`, `manifest.*`, a second ticket) is dropped |
| Any other `/v1/master/` or `/v1/dash/` path | Nothing: 400 |
| `/v1/session…` (explicit session initialization) | Nothing: 403. A `POST` gets CloudFront's 403 too, because the behaviour allows only GET, HEAD and OPTIONS |
| Other `/v1/*` paths (media playlists, segments, beacons) | The request with its query: MediaTailor reads player parameters at init only |

The function empties the query before anything else and catches every error, because a function that throws makes
CloudFront answer 503 to the viewer. `<prefix>-identity-edge-errors` fires on the first execution error. Its source's
tests run the crafted events of the design's conformance checks in Node.js (`infra/tests/test_edge_rebuild.py`), and
the end-to-end test runs them on the deployed function with `TestFunction`.

With the module on, the route needs another CDN to do the same (the path contract's `viewer_request`).

## The PRE_SESSION redeem

Each pair sibling of a monetized channel is **ticketed**: its ADS request carries `X-Fast-T: {{player_params.t}}`, and
its PRE_SESSION root runs `<p>_<sibling>_ps_redeem` before the session controls. The redeem POSTs
`{t, sid: session.uuid, pc: "<the sibling's name>"}` to `POST /redeem` with an 800 ms timeout. It is never cached
(cache scope `none`: a single-use call), and a session without a ticket skips it. Its answer goes to `temp.redeem`,
which the root does not commit, so a 409 or a timeout changes nothing in the session.

Not ticketed: the probe (`-p-z`), the lab channel's siblings (they publish their hook events, which would log the
ticket), a child-directed channel's siblings (KIDS never has a viewer id), and the siblings of the experimental
ad-server route. Their sessions are CONTEXTUAL, SAFE or KIDS as before.

## The Pod Decision Service

On every break the fast-ctv module sends the ticket to `/policy` as `t`. With the module on, the sidecar reads
`T#<t>` with a consistent read and decides (`fast_channels.core.identity.binding`, `fast_channels.pds.bindings`):

| The row | The break |
|---|---|
| Bound to this session (`redeemedBy`) and this configuration (`pc`), last seen under 2 hours ago, bound under 24 hours ago | The viewer's: the mode follows the row's privacy mode (`PSEUDONYMOUS` today), reason `bound` |
| Not redeemed, inside its redeem window | Bound to this session by the sidecar itself (the same conditional update as `POST /redeem`), then the viewer's |
| Bound to another session | CONTEXTUAL, `replay` |
| For another configuration | CONTEXTUAL, `pc_mismatch` |
| Not redeemed, window passed; or the lease ended | CONTEXTUAL, `expired_or_raced` |
| None | CONTEXTUAL, `unknown` |
| No ticket, or a malformed one | CONTEXTUAL, `missing` or `invalid`, without a lookup |
| The read failed (timeout, throttle) | SAFE, `consent_unavailable`: own book and house ads only, because it may be a signed-in viewer |

A bound row's `last_seen` is renewed at most every 10 minutes. The sidecar counts `BindingLookups` (by reason),
`BindingReplays`, `BindingSelfRedeems` and `BindingErrors` in its metrics.

Each `pds.policy` decision record of a bound viewer's break carries `vid` and the ticket's `cid`; every other record
has them `null`. The `vid` never reaches Prebid Server or a bidder: the `/policy` answer has no viewer id. The lake
keeps the records for its retention.

## Residual risk

MediaTailor's endpoints are public. A caller who skips the CDN reaches MediaTailor's session init directly, can set
`aws.*` parameters, and gets an ad-free session if they also make the PRE_SESSION hook fail. The edge rebuild closes
that path for everyone who comes through the CDN; the ops module's zero-ad-sessions detector and the hook alarms
detect the rest. Both are listed in [Workarounds to remove](workarounds.md) (`W-2 FR-1`), to delete when MediaTailor
offers authenticated session initialization or a setting that ignores player-set `aws.*`.

## Demo viewers

```sh
uv run python scripts/seed_personas.py --prefix <prefix>                    # the sign_in: user personas of contracts/personas.json
uv run python scripts/seed_personas.py --prefix <prefix> --persona dana     # or name them
uv run python scripts/seed_personas.py --prefix <prefix> --delete
```

Each persona becomes the user `<id>@personas.example.com` with `custom:persona=<id>` and a generated password. The
guest and kids profiles get no user. The passwords are in the Secrets Manager secret `<prefix>/identity/demo-users`,
as `{"<persona>": {"email": ..., "password": ...}}`; a re-run keeps them. The script prints each persona's `vid`.

To sign a persona in from a script or a test, read the secret, take the client id from the SSM parameter
`/<prefix>/identity/client-id`, and call Cognito `InitiateAuth` with `USER_PASSWORD_AUTH`. Send the result's
`AccessToken`.

## Logs

- The mint logs the correlation id, the channel and the configuration. It never logs the ticket, the `vid` or a claim.
- The API's access log holds the request id, time, method, path, status and latencies. It holds no address, header,
  body or claim.
- **The access log needs the account's API Gateway CloudWatch role.** A REST API writes its logs through one role
  per account and Region, the API Gateway account setting `cloudwatchRoleArn`. The stack does not set it, because
  that would take the setting over from every other REST API in the account. Without the role, or when it names a
  role that no longer exists, the deployment succeeds and the API serves requests, but it writes no access log.
  Check it before you rely on the log:

  ```sh
  aws apigateway get-account --query cloudwatchRoleArn       # a role that exists, trusted by apigateway.amazonaws.com
  ```

  To set it, create a role that `apigateway.amazonaws.com` may assume, with the managed policy
  `AmazonAPIGatewayPushToCloudWatchLogs`, and run `aws apigateway update-account --patch-operations
  op=replace,path=/cloudwatchRoleArn,value=<role ARN>`. This is an account-wide change.
- **The CDN's access logs** leave out `cs-uri-query`, `c-ip` and `x-forwarded-for` with the module on. CloudFront
  records the viewer's original query, before the edge rebuild, so it would hold the ticket; and the viewer's address
  beside the session id in the path would tie it to a `vid` through the decision records. The lake never read these
  fields.
- The siblings' ADS log deliveries already leave out `requestHeaders` (which holds `X-Fast-T`), and a ticketed sibling
  never opts in to the hook events that log the redeem's body (validator R-P2).
- The sidecar logs a failed lookup with the decision id, never the ticket.

## Cost

- Cognito Essentials: free for the first 10,000 monthly active users, then billed per user.
- The KMS key: $1 a month, plus $0.03 per 10,000 `GenerateMac` calls (one per minted ticket).
- API Gateway REST, Lambda and DynamoDB on demand: fractions of a cent per thousand plays (a mint and a redeem each).
- The edge rebuild: CloudFront Functions bill $0.10 per million invocations, on every `/v1/*` request (playlists and
  ad segment redirects, a few per segment duration per session).
- The ticket lookup: one consistent DynamoDB read per break of a ticketed session, and a write every 10 minutes.
- One alarm ($0.10 a month).
