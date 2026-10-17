# End-to-end tests

The suite checks a deployed stage the way its users meet it. It ingests a file and schedules it with the agent's
tools, airs it on the channel, plays it through the CDN, and asks the agent a question. A green run is the release
gate (CONTRIBUTING.md). The manual GitHub workflow `.github/workflows/e2e.yml` runs it between a deploy and a destroy.

```bash
uv run pytest tests/e2e -m e2e --prefix <prefix> -v      # against FastChannels-<prefix>-* (about 40 minutes)
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
- The machine client, for `test_05_agent`: it signs in with Cognito `client_credentials`, and the client exists
  only with an autopilot or `agent.machine_client`. Deploy with `-c fast-channels.agent.machine_client=true`
  (the workflow does); without it the test is skipped.
- AWS credentials allowed to:
  - read the stacks (`cloudformation:DescribeStacks`);
  - put objects into the ingest bucket, and read the media bucket and the media library table;
  - call `states:DescribeExecution`, `lambda:InvokeFunction` on the tool functions,
    `mediatailor:Describe*`/`ListAlerts`, `cognito-idp:DescribeUserPoolClient`, and `logs:FilterLogEvents` on
    Channel Assembly's as-run log group `MediaTailor/Channel/AsRunLog`.

  The deploying role has all of these.
- `polly:SynthesizeSpeech`, for the speaking clip of `test_08_generated_captions` (skipped on stages whose
  `CaptionEngine` output is `none`).
- `ffmpeg` on the `PATH`, to render the synthetic sources.
- Outbound HTTPS to the CDN, Cognito and Amazon Bedrock AgentCore.

A run costs about 1.50 US dollars. Most of that is the MediaConvert encodes (about $0.65 for the 70 s pattern and
$0.25 for the 24 s speaking clip with the standard ladder, plus its caption scan and captioning proxy). The rest is about fifteen minutes
of a BASIC channel, one agent turn, about 45 s of Elemental Inference smart subtitles ($0.11), and cents of
CloudFront, S3 and logs.

## What runs, in order

Session fixtures (`conftest.py`) do each slow step once, when a test first needs it. A test only asserts.

| Module | Step | Checks |
|---|---|---|
| `test_01_outputs` | Read the stacks | Every output the scripts and the console rely on is present and well formed. The keys are listed in `scripts/_stack.py` `OUTPUT_KEYS`. |
| `test_02_ingest` | Render a 70 s 1080p30 `testsrc2` pattern with a 1 kHz tone and a WebVTT file with a cue every 5 s. Upload the captions as `<asset>.en.vtt`, its sidecar (`asset_id` e2e-pattern-<UTC timestamp>, `content_id` e2e-pattern, `cue_points_ms` [32000]), then the file, to the ingest bucket. Wait until the asset is READY. | Every rendition profile manifest is in S3, with the ladder's exact signature (caption tracks included). The break offsets include the snapped cue point (32032 ms at 29.97 fps, off the segment grid, so the encode has an ESAM split). The VOD source has a package configuration for every source group, every channel output among them, each pointing at an existing manifest. The English track came from the provided file, its IMSC segments have the video's durations (Finalize re-cut them after the split) and carry the cues. |
| `test_03_ladder_kit` | — | The default ladder is READY. Its filler and break-slate VOD sources exist. The VAST pod lists the house ads. Every creative, `b.gif` and an SGAI ad segment are readable through the CDN. |
| `test_04_channel` | Call `schedule___plan` and `schedule___publish` for three programs, starting in about 2.5 minutes, with one 30 s break each. Then `channels___start_channel`. The tool Lambdas are invoked the way AgentCore Gateway invokes them (`bedrockAgentCoreToolName` in the client context). One SSAI, one SGAI and one DASH session then watch the breaks air. | The SSAI multivariant lists every codec family (`avc1`, `hvc1`, `av01`). A media playlist per family and per audio group plays. Each single-codec output carries only its codec. SSAI stitches ads from the second break on. SGAI shows an `com.apple.hls.interstitial` DATERANGE, and its asset lists carry ads that are readable via `/preconditioned-ads/*`. The DASH MPD is dynamic, uses its output's codec, and gets ad periods. Neither the channel nor the VOD source has a `SCHEDULING_ERROR`. The channel lists the ladder's caption tracks; their content segments sit on the video grid and SSAI ads carry MediaTailor's blank WebVTT; SGAI ad multivariants list the same tracks; every DASH period has the IMSC text set, and a DASH ad init segment resolves to media. |
| `test_05_agent` | Get a client-credentials token for the autopilot app client. Send one AG-UI `RunAgentInput`, "List the channels.", to the runtime. | `RUN_STARTED` … `RUN_FINISHED`, a `channels___list_channels` tool call, and no `RUN_ERROR`. |
| `test_06_stop` | `channels___stop_channel` | The channel is STOPPED. The session teardown stops it again, whatever failed before. |
| `test_07_late_start` | With the channel stopped, publish two programs starting in 3 minutes, with one 2-minute break each. Start the channel 30 s after the first program's start. Stop it at the end of the module. | `channels___start_channel` reports the first program in `skipped` (and only programs whose start had passed), the media library marks it `SKIPPED`, and the as-run log shows the second program starting at its planned time, within one segment, and no start of the first. |
| `test_08_generated_captions` | Render a 24 s pattern that speaks (Amazon Polly) and upload it without captions (`captions.spoken_language` en). Wait until READY. | The English track was generated by the stage's engine (`CaptionEngine`), the file under `captions/` transcribes the speech starting near 2 s, and the encode's IMSC segments carry it. |

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

- Two assets per run, `e2e-pattern-<UTC timestamp>` (content id `e2e-pattern`) and `e2e-pattern-spoken-<UTC
  timestamp>`: their encodes under `assets/`, their VOD sources, their uploads (and the provided captions) under
  `e2e/` and the generated captions under `captions/` in the ingest bucket. Each run uses a new asset id because the pipeline
  refuses to re-encode a `READY` asset in place (that would overwrite the media its VOD source serves). The stage's
  reaper removes these VOD sources when the stage is destroyed; on a long-lived stage, delete old ones as needed.
- Its five programs (the late start's first one `SKIPPED`). Channel Assembly deletes aired programs about 15
  minutes later. Their EPG entries stay in the media library and in `epg/<channel>.json`.
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
