# End-to-end tests

The suite checks a deployed stage the way its users meet it. It ingests a file and schedules it with the agent's
tools, airs it on the channel, plays it through the CDN, and asks the agent a question. A green run is the release
gate (CONTRIBUTING.md). The manual GitHub workflow `.github/workflows/e2e.yml` runs it between a deploy and a destroy.

```bash
uv run pytest tests/e2e -m e2e --prefix <prefix> -v      # against FastChannels-<prefix>-* (about 40 minutes)
uv run pytest tests/e2e -m e2e -v                        # the prefix in cdk.json (fast-channels.prefix)
uv run pytest tests/e2e -m e2e --prefix <prefix> -k agent   # one part only
uv run pytest tests/e2e/test_12_release.py -m e2e --prefix <prefix> --releases -v   # the release module's rollouts (M3-04)
uv run pytest tests/e2e -m "not e2e"                     # offline unit tests of the helpers and scripts
```

If the stage's stacks do not exist, or there are no AWS credentials, every e2e test is skipped and the reason is
given. The report header prints the run id. That id is the correlation id of the ingest and of every tool call, so
`uv run python scripts/trace.py <run id>` shows everything the run caused, in order.

## Requirements

- A deployed stage with its default ladder kit READY. The kit is provisioned during the deployment. Run in the
  stage's region (`AWS_REGION`). The shipped IaC channel is named `{prefix}-demo`, so it follows any prefix:
  `npx cdk deploy '**' -c fast-channels.prefix=<prefix>`.
- The machine client, for `test_05_agent`: it signs in with Cognito `client_credentials`, and the client exists
  only with an autopilot or `agent.machine_client`. Deploy with `-c fast-channels.agent.machine_client=true`
  (the workflow does); without it the test is skipped.
- AWS credentials allowed to:
  - read the stacks (`cloudformation:DescribeStacks`);
  - put objects into the ingest bucket, and read the media bucket and the media library table;
  - call `states:DescribeExecution`, `lambda:InvokeFunction` on the tool functions,
    `mediatailor:Describe*`/`Get*`/`ListAlerts`, `cognito-idp:DescribeUserPoolClient`, `logs:FilterLogEvents` on
    Channel Assembly's as-run log group `MediaTailor/Channel/AsRunLog`, `logs:GetDeliverySource`,
    `logs:DescribeDeliveries` and `logs:ListTagsForResource`, and `lambda:GetFunctionConfiguration` on the
    channels tool function.

  The deploying role has all of these.
- `ffmpeg` on the `PATH`, to render the synthetic source.
- Outbound HTTPS to the CDN, Cognito and Amazon Bedrock AgentCore.

A run costs about 0.58 US dollars. The MediaConvert encode of the 70 s pattern is about $0.07 with the standard
ladder (three outputs, `fast_channels.core.cost`): its cue point needs ESAM, which bills the job at the Professional
tier. The rest is about fifteen minutes of a BASIC channel,
one agent turn, and cents of CloudFront, S3 and logs.

## What runs, in order

Session fixtures (`conftest.py`) do each slow step once, when a test first needs it. A test only asserts.

| Module | Step | Checks |
|---|---|---|
| `test_01_outputs` | Read the stacks | Every output the scripts and the console rely on is present and well formed. The keys are listed in `scripts/_stack.py` `OUTPUT_KEYS`. |
| `test_02_ingest` | Render a 70 s 1080p30 `testsrc2` pattern with a 1 kHz tone. Upload its sidecar (`asset_id` e2e-pattern-<UTC timestamp>, `content_id` e2e-pattern, `cue_points_ms` [32000]), then the file, to the ingest bucket. Wait until the asset is READY. | The manifest of every rendition profile (the one `hls` profile) is in S3 with the ladder's exact signature: for the standard ladder, three AVC variants, each with the muxed AAC-LC in `CODECS`. It has no checklist findings and `CLOSED-CAPTIONS=NONE` on every variant. Every variant is MPEG-TS (`.ts` segments served as `video/MP2T`) with identical EXTINFs. The cue point snaps to the nearest frame (`Grid.snap_to_frame`: 31999 ms at 29.97 fps), off the 6006 ms segment grid, so the encode used ESAM: every variant has a segment boundary there (cumulative EXTINFs, within 1 ms) and the segment that ends there is shorter than 6.006 s. The asset's cue point is the snapped value and its one break offset is that boundary. The estimated encode cost it records is at the Professional rate. The VOD source has an HLS package configuration for every source group, every channel output among them, each pointing at an existing manifest. |
| `test_03_ladder_kit` | — | The default ladder is READY. Its filler and break-slate VOD sources exist. The VAST pod lists the house ads, each with one streaming HLS MediaFile: the creative's playlists under `ads/<ladder>/<creative id>/`, whose multivariant has the ladder's signature (preconditioned ads must match the content's variants) and whose media playlists point at the house ad's own `.ts` segments with absolute URIs on the CDN. Every creative, `b.gif` and an ad segment are readable through the CDN. |
| `test_04_channel` | Call `schedule___plan` and `schedule___publish` for three programs, starting in about 2.5 minutes, with one 60 s break each. Then `channels___start_channel`. The tool Lambdas are invoked the way AgentCore Gateway invokes them (`bedrockAgentCoreToolName` in the client context). An SSAI session then watches the breaks air in every variant. | The SSAI multivariant lists the ladder's variants with their audio muxed (no `EXT-X-MEDIA`). The media playlist of every variant plays, its content in `.ts` segments. Every variant has stitched house ads from the second break on. Every break starts exactly at the frame-snapped cue point, in every variant: Channel Assembly's ad marker (the SCTE-35 `EXT-X-DATERANGE`) follows the pattern's segment that ends at the cue in the encode (the ESAM split), and its `START-DATE` is that segment's end and the first break segment's program date-time (within 1 ms). Neither the channel nor the VOD source has a `SCHEDULING_ERROR`. |
| `test_05_agent` | Get a client-credentials token for the autopilot app client. Send one AG-UI `RunAgentInput`, "List the channels.", to the runtime. | `RUN_STARTED` … `RUN_FINISHED`, a `channels___list_channels` tool call, and no `RUN_ERROR`. |
| `test_06_stop` | `channels___stop_channel` | The channel is STOPPED. The session teardown stops it again, whatever failed before. |
| `test_07_late_start` | With the channel stopped, publish two programs starting in 3 minutes, with one 2-minute break each. Start the channel 30 s after the first program's start. Stop it at the end of the module. | `channels___start_channel` reports the first program in `skipped` (and only programs whose start had passed), the media library marks it `SKIPPED`, and the as-run log shows the second program starting at its planned time, within one segment, and no start of the first. |
| `test_08_agent_channel` | With the IaC channel left alone, `channels___create_channel` creates an agent channel `<prefix>-agent-e2e-<hex>` on the IaC channel's ladder. It is never started. Then `channels___delete_channel` deletes it. | The channel is `STOPPED` with AS_RUN logs; its `STITCHED_ONLY` playback configuration logs with `VENDED_LOGS` at the deployment's percentage; the channel policy admits only that configuration (with `modules.release`, also its siblings `<configuration>-p-?`). Every log type has its delivery source and exactly one delivery to the deployment's destination. The channel, the configuration, the sources and the deliveries carry `fast:app`, `fast:owner=agent`, the ladder and the run's correlation id. After the delete, none of them, no media library item, no EPG feed and no listing remains. |

With a module on, the stage also runs that module's tests (each skipped on a stage without the module's stack):

| Module | Step | Checks |
|---|---|---|
| `test_08_ops` (`modules.ops`) | Wait up to 15 minutes for the lake's log copies and 7 minutes for the census | The Ops stack outputs exist. The lake's copies of the ADS logs hold the run's `MAKING_ADS_REQUEST` events, and no ADS record has `requestHeaders` or `rawAdsRequest`, nor any manifest record `mediaTailorPath`, `input` or `http`. `v_breaks` (Athena) returns every break of the run's three programs by its SCTE-35 event id, each seen by a session. The bus log has a `Census Changed` event from 0 to running and one from running to 0. |
| `test_09_ledger` (`modules.ops`) | Append 20 entries over 2 chains with the producers' client; wait up to 5 minutes for the archive | A repeated append is refused. Every record is archived and the chains verify against their heads (`scripts/ledger_verify.py`). The newest archive object is under Object Lock with a future retain-until date. |
| `test_11_demand_edge` (`modules.demand`, after `make deploy-demand`; `harness_demand.py`) | MediaTailor's POST through the guidance's CloudFront and WAF, with origin secret A and `X-Fast-Probe: 1`; a 100/s × 6 s herd through the warm-up project | Every grid duration (30/60/90/120 s) answers a VAST 4.2 pod as `application/xml` (empty until the Pod Decision Service). 31 s and 300 s get 400. Without the secret, with a wrong one, or on another path: 403; `/status` passes. An account without the module gets Prebid Server's JSON and the simulator logs no bid request. The herd is all 200 with p99 under 1 s. |
| `test_12_release` (`modules.release`, after `deploy-demand`) | One invocation of the drift check and one of the log reconciliation (one more `UnloggedAdsRequests` datapoint) | Every sibling the Release stack names posts to Prebid Server (`POST` with a body, the origin header, `FOLLOW_AD_SEQUENCE`), with threshold 121, `BEHIND_LIVE_EDGE` avail suppression, the controller's tags and a FunctionMapping whose every hook's root is of its own release, and whose Functions exist. Each sibling has five vended-log deliveries, none of an ads or manifest log with the fields that may hold a secret, and the siblings' log group is KMS-encrypted. Each channel's `/plan/playback/<channel>.json`, read through the CDN with `max-age=30`, names one of its siblings. The drift check finds no drift. The log reconciliation reports every sibling, its counted and logged requests printed (a shortfall is a MediaTailor log gap: printed, not failed); on a stage younger than the hour it reconciles, it reports the hour skipped. The ledger has `release.accept` and each channel's `release.apply`. |
| `test_12_release` with `--releases` (about 50 minutes; `harness_release.py`) | A throwaway stack `<prefix>-e2e-release-rig`: an origin behind an API Gateway HTTP API (low throttling; a key per run, sent in a header by the Function step and the ADS request, in the path by the VOD source; no Lambda permission open to everyone) with a VOD source with ad markers, an empty VAST and a lookup whose latency the test sets (cached per break) and a stub mint that reads the release pointer with `Entity-Id`, as M4's mint will. The VOD configuration `<prefix>-lab-x` (no Channel Assembly, outside the siblings' pattern) with an unquoted, unresolved body variable, its ads log delivered to the module's group; a VOD session every 10 s. Then the first IaC channel on air with a break a minute, and two releases of the stage's bundle plus a PRE_ADS step that calls the lookup, published as `Custom::DesiredState` publishes one | `<prefix>-lab-x-markers-without-ads-request` goes to ALARM within 5 minutes; the configuration logged markers and no ad request, and `AdDecisionServer.Errors` stayed 0. **Rollback during a rollout:** the lookup answers at once, so the smoke test and the soak pass and the pointer deploys; the mint's share of the candidate tracks the deployment's percentage; three sessions open on the candidate when the mint first names it; at the second step the lookup turns slow (2,500 ms, P0-08's failure): its 1,000 ms step times out, which no error metric counts. The pointer poll stops the deployment (its `Function.Latency` reached the timeout in two minutes), the execution ends `RolledBack` with `timed_out` naming the lookup, the candidate maps its last good tree again, the plan names the stable sibling, the mint names it for every viewer, and the ledger has `release.accept`, `release.point` and `release.rollback` (and the channel's `release.apply`). Each session had a break while the lookup was slow; the lookup's latency reached its timeout before the remap, and the release's lookup ran on no break after it; no break failed after the remap. **Smoke gate:** with the lookup slow from the start, the execution ends `RolledBack` before any pointer deployment, the smoke's cause naming the lookup's timeout. **Rotation:** with three sessions on the stable sibling, `<prefix>-release-rotation` succeeds; both origin secrets have new values, the WAF admits exactly them, every sibling sends the current one, no session's ad request was refused by the origin gate (403), breaks were requested and filled, and `fast-origin-required` blocked nothing. |
| `test_99_model_cap` (`modules.models`, a `scheduler.plan` cap of at most $0.05, the machine client) | One agent turn; then the registry entry's cap is lowered below the day's counted spend, two more turns, and the entry is put back | The first turn finishes and is counted at list prices. Both later turns end with `RUN_ERROR` `MODEL_CAP_REACHED` and spend nothing, the counter item records the notification, and the runtime logs the cap notice once. It runs last because it spends the stage's agent budget for the day. |

Timing and behaviour worth knowing:

- **The first break of a new creative airs its slate.** MediaTailor imports a creative the first time it sees it
  (`NEW_CREATIVE`, e2e.md §8.11). This is why ads are asserted only from the second break on. The watch ends as
  soon as every variant has shown ads, and at the latest 45 s after the last break.
- **Right after a deployment the agent may list no tools.** Gateway policies can still be propagating, and a fresh
  runtime caches the empty list. The agent test therefore retries with a new conversation (a new runtime session)
  every minute, for up to seven minutes.
- Every HTTP request is retried with backoff on transport errors, 429 and 5xx. A new MediaTailor session can answer
  504 while its origin warms up. Tool calls are retried while the tool reports a retryable error, such as a
  schedule lease that is held. Every wait has a deadline and fails with the last state it saw.

## What a run leaves behind

- One asset per run, `e2e-pattern-<UTC timestamp>` (content id `e2e-pattern`): its encode under `assets/`, its VOD
  source, and its upload and sidecar under `e2e/` in the ingest bucket. Each run uses a new asset id because the
  pipeline refuses to re-encode a `READY` asset in place (that would overwrite the media its VOD source serves). The
  stage's reaper removes these VOD sources when the stage is destroyed; on a long-lived stage, delete old ones as
  needed.
- Its five programs (the late start's first one `SKIPPED`). Channel Assembly deletes aired programs about 15
  minutes later. Their EPG entries stay in the media library and in `epg/<channel>.json`.
- The channel, stopped.
- With `--releases`: the releases (rolled back; the candidate sibling keeps its tree), their Function trees (deleted
  by a later promotion's collection), their bundles in the release bucket and their ledger records. The rig and
  `<prefix>-lab-x` are deleted at the end of the module, whatever failed. If the rollout was promoted instead (a
  failure), deploy the stage's bundle again as a new release: `aws stepfunctions start-execution` with the input of
  [docs/release-controller.md](../../docs/release-controller.md#the-apply-path) and a new `correlation_id`.

`make destroy` removes all of it. If a destroy fails, `uv run python scripts/cleanup.py --prefix <prefix>` lists
the runtime resources that are left, and `--apply` deletes them.

## Layout

- `conftest.py`: the `--prefix` option, the skip logic and the session fixtures.
- `harness.py`: the scenario constants; waiting and retries; the tool invoker; the player (HTTP); the playback
  watcher; the agent client; the test-pattern renderer.
- `playback.py`: pure readers for HLS (segments, program date-times, ad markers) and VAST. Unit-tested against
  `data/`: `esam-ts/`, the media playlists of the MediaConvert TS job that verified ESAM (cue points at 13480 and
  26994 ms), and `synthetic-*` playlists. Those keep the MediaTailor URL shapes of the research run (e2e.md), with
  the muxed TS variants, segment names and 6.006 s durations of the real encode in `core/tests/data/hls-ts/` (no
  `EXT-X-MAP`). Each says so in a comment line. Replace them with captures from a deployed TS stage.
- `agui.py`: the AG-UI request and the Server-Sent Events reader.
- `test_harness.py` and `test_scripts.py`: offline tests of the above and of `scripts/` (botocore Stubber,
  `httpx.MockTransport`). They are not marked `e2e`.
