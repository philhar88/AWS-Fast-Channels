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

`make deploy` then adds the stack `FastChannels-<prefix>-Identity`.

**Not yet on the playback path.** This release deploys the pool, the mint and the redeem. The CDN's edge rebuild, the
siblings' PRE_SESSION redeem and the Pod Decision Service's binding check come next. Until then the CDN does not
rebuild the query, nothing redeems a ticket, and a minted URL plays like the anonymous one, CONTEXTUAL.

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

## Cost

- Cognito Essentials: free for the first 10,000 monthly active users, then billed per user.
- The KMS key: $1 a month, plus $0.03 per 10,000 `GenerateMac` calls (one per minted ticket).
- API Gateway REST, Lambda and DynamoDB on demand: fractions of a cent per thousand plays.
