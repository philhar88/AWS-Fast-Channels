# End-to-end tests

The suite checks a deployed stage the way its users meet it. It ingests a file and schedules it with the agent's
tools, airs it on the channel, plays it through the CDN, and asks the agent a question. A green run is the release
gate (CONTRIBUTING.md). The manual GitHub workflow `.github/workflows/e2e.yml` runs it between a deploy and a destroy.

```bash
uv run pytest tests/e2e -m e2e --prefix <prefix> -v      # against FastChannels-<prefix>-* (about 30 minutes)
uv run pytest tests/e2e -m e2e -v                        # the prefix in cdk.json (fast-channels.prefix)
uv run pytest tests/e2e -m e2e --prefix <prefix> -k agent   # one part only
uv run pytest tests/e2e -m "not e2e"                     # offline unit tests of the helpers and scripts
```

If the stage's stacks do not exist, or there are no AWS credentials, every e2e test is skipped and the reason is
given. The report header prints the run id. That id is the correlation id of the ingest and of every tool call, so
`uv run python scripts/trace.py <run id>` shows everything the run caused, in order.

## Requirements

- A deployed stage with its default ladder kit READY. The kit is provisioned during the deployment. Run in the
  stage's region (`AWS_REGION`). The shipped IaC channel is named `{prefix}-demo`, so it follows any prefix:
  `npx cdk deploy '**' -c fast-channels.prefix=<prefix>`.
- An autopilot schedule on that channel, for `test_05_agent`: it signs in as the autopilot's machine client, which
  exists only for a channel with an `autopilot_cron`. The workflow deploys with
  `-c 'fast-channels.channels.iac.0.autopilot_cron=cron(0 0 1 1 ? *)'`, which fires only on 1 January.
- No `e2e-pattern` asset in the stage yet (see [What a run leaves behind](#what-a-run-leaves-behind)).
- AWS credentials allowed to:
  - read the stacks (`cloudformation:DescribeStacks`);
  - put objects into the ingest bucket, and read the media bucket and the media library table;
  - call `states:DescribeExecution`, `lambda:InvokeFunction` on the tool functions,
    `mediatailor:Describe*`/`ListAlerts` and `cognito-idp:DescribeUserPoolClient`.

  The deploying role has all of these.
- `ffmpeg` on the `PATH`, to render the synthetic source.
- Outbound HTTPS to the CDN, Cognito and Amazon Bedrock AgentCore.

A run costs about one US dollar. Most of that is the MediaConvert encode (about $0.65 for 70 s with the standard
ladder). The rest is about ten minutes of a BASIC channel, one agent turn, and cents of CloudFront, S3 and logs.

## What runs, in order

Session fixtures (`conftest.py`) do each slow step once, when a test first needs it. A test only asserts.

| Module | Step | Checks |
|---|---|---|
| `test_01_outputs` | Read the stacks | Every output the scripts and the console rely on is present and well formed. The keys are listed in `scripts/_stack.py` `OUTPUT_KEYS`. |
| `test_02_ingest` | Render a 70 s 1080p30 `testsrc2` pattern with a 1 kHz tone. Upload its sidecar (`content_id` e2e-pattern, `cue_points_ms` [30000]), then the file, to the ingest bucket. Wait until the asset is READY. | Every rendition profile manifest is in S3, with the ladder's exact signature. The break offsets include the snapped cue point (30030 ms at 29.97 fps). The VOD source has a package configuration for every source group, every channel output among them, each pointing at an existing manifest. |
| `test_03_ladder_kit` | — | The default ladder is READY. Its filler and break-slate VOD sources exist. The VAST pod lists the house ads. Every creative, `b.gif` and an SGAI ad segment are readable through the CDN. |
| `test_04_channel` | Call `schedule___plan` and `schedule___publish` for three programs, starting in about 2.5 minutes, with one 30 s break each. Then `channels___start_channel`. The tool Lambdas are invoked the way AgentCore Gateway invokes them (`bedrockAgentCoreToolName` in the client context). One SSAI, one SGAI and one DASH session then watch the breaks air. | The SSAI multivariant lists every codec family (`avc1`, `hvc1`, `av01`). A media playlist per family and per audio group plays. Each single-codec output carries only its codec. SSAI stitches ads from the second break on. SGAI shows an `com.apple.hls.interstitial` DATERANGE, and its asset lists carry ads that are readable via `/preconditioned-ads/*`. The DASH MPD is dynamic, uses its output's codec, and gets ad periods. Neither the channel nor the VOD source has a `SCHEDULING_ERROR`. |
| `test_05_agent` | Get a client-credentials token for the autopilot app client. Send one AG-UI `RunAgentInput`, "List the channels.", to the runtime. | `RUN_STARTED` … `RUN_FINISHED`, a `channels___list_channels` tool call, and no `RUN_ERROR`. |
| `test_06_stop` | `channels___stop_channel` | The channel is STOPPED. The session teardown stops it again, whatever failed before. |

Timing and behaviour worth knowing:

- **The first break of a new creative airs its slate.** MediaTailor imports a creative the first time it sees it
  (`NEW_CREATIVE`, e2e.md §8.11). This is why ads are asserted only from the second break on. The watch ends as
  soon as every session has seen its ads, and at the latest 45 s after the last break.
- **Right after a deployment the agent may list no tools.** Gateway policies can still be propagating, and a fresh
  runtime caches the empty list. The agent test therefore retries with a new conversation (a new runtime session)
  every minute, for up to seven minutes.
- Every HTTP request is retried with backoff on transport errors, 429 and 5xx. A new MediaTailor session can answer
  504 while its origin warms up. Tool calls are retried while the tool reports a retryable error, such as a
  schedule lease that is held. Every wait has a deadline and fails with the last state it saw.

## What a run leaves behind

- The asset `e2e-pattern`: its encode under `assets/e2e-pattern/`, its VOD source, and its upload
  `e2e/e2e-pattern.mp4` (with `.json`) in the ingest bucket. The pipeline refuses to encode a `READY` asset again
  in place (it would overwrite the media its VOD source serves), so a second run against the same stage fails at
  ingest with `IngestRefusedError … is already READY`. Run the suite against a fresh stage, as the workflow does.
- Its three programs. Channel Assembly deletes aired programs about 15 minutes later. Their EPG entries stay in the
  media library and in `epg/<channel>.json`.
- The channel, stopped.

`make destroy` removes all of it. If a destroy fails, `uv run python scripts/cleanup.py --prefix <prefix>` lists
the runtime resources that are left, and `--apply` deletes them.

## Layout

- `conftest.py`: the `--prefix` option, the skip logic and the session fixtures.
- `harness.py`: the scenario constants; waiting and retries; the tool invoker; the player (HTTP); the playback
  watcher; the agent client; the test-pattern renderer.
- `playback.py`: pure readers for HLS, HLS Interstitials, DASH and VAST. Unit-tested against `data/`, which holds
  trimmed wire captures from the research run (e2e.md).
- `agui.py`: the AG-UI request and the Server-Sent Events reader.
- `test_harness.py` and `test_scripts.py`: offline tests of the above and of `scripts/` (botocore Stubber,
  `httpx.MockTransport`). They are not marked `e2e`.
