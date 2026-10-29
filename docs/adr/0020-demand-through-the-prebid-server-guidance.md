# 0020 — Programmatic demand through the AWS Prebid Server guidance

**Status:** Proposed (amends [0002](0002-l2-constructs-over-solutions-constructs.md), exception A). It is accepted when
the demand and Pod Decision Service modules pass their end-to-end legs.

## Context
The optional demand module ([docs/demand.md](../demand.md)) puts an auction behind the channel's ad breaks.

- MediaTailor can send its ad decision server request as a templated HTTP POST: a URL, header templates and a body
  template filled with session and SCTE-35 variables. It accepts a VAST XML reply.
- AWS publishes the [Guidance for Deploying a Prebid Server on
  AWS](https://github.com/aws-solutions-library-samples/prebid-server-deployment-on-aws) (Apache 2.0, CDK Python with
  AWS Solutions Constructs): Prebid Server Java on Fargate behind CloudFront, AWS WAF and an ALB, with a bidder
  simulator. Release v1.4.0 deploys about 350 resources.
- In live tests on a Channel Assembly channel (October 2026), MediaTailor's POST went through the guidance's
  CloudFront and WAF to its Prebid Server 3.43.0. A small Prebid Server module answered with one VAST 4.2 pod, and
  MediaTailor stitched it in `sequence` order. MediaTailor measured p50 189 ms and p95 298 ms per POST.
- **MediaTailor inserts template values raw, without JSON escaping**, although its user guide says it escapes them.
  A `"` or `\` in a value, or an unresolved unquoted number, makes the body invalid. MediaTailor then sends no
  request at all and logs only `AD_MARKER_FOUND`, and the break plays unfilled. `PutPlaybackConfiguration` accepts a
  body template that is not JSON.
- Prebid Server copies no client header into bidder requests, except `Sec-GPC`, `Save-Data` and `Sec-CH-UA*`.
- As shipped, the guidance:
  - hard-codes its stack names and the default CDK bootstrap qualifier;
  - has no extension point for customer WAF rules, and deletes its web ACL with the lock token from creation, so any
    later `UpdateWebACL` breaks stack deletion;
  - ships simulator creatives (5, 15 and 30 s progressive MP4s with one advertiser domain) that cannot fill a CTV pod;
  - runs one Vert.x event loop per 2-vCPU task (every auction ran on one thread);
  - lets a forced deployment, its documented way to load a new configuration, run at 50% of the desired tasks.

## Decision
- **The guidance is deployed as its own CDK app**, not composed into this repository's app (exception A to
  [0002](0002-l2-constructs-over-solutions-constructs.md)). It runs at a pinned tag and commit
  (`vendor/prebid-guidance/PIN.toml`, with the SHA-256 of every upstream file the overlays patch or rely on), with
  these overlays:

  | Overlay | Change |
  |---|---|
  | O1 | Stack-name prefix and a dedicated CDK bootstrap per stage, from CDK context |
  | O2 | Stack parameter `ExtraWafRuleGroupArn`: a customer rule group referenced first by the guidance's web ACL |
  | O4 | The fast-ctv module compiled through the guidance's `amt-bidder` injection point |
  | O5 | Bidder simulator creatives from configuration (30.03 s HLS creatives preconditioned on the ladder) |
  | O7 | The Prebid Server service deploys at 100% minimum healthy and 200% maximum |

  `scripts/prebid_guidance.py` checks the pinned commit out, refuses it if the tag moved, a pinned file changed or a
  patch does not apply, then applies the overlays. The repository reads only the parameters the deploy script writes
  under `/<prefix>/demand/`. CI applies the overlays to the pinned tag on every run.
- **MediaTailor POSTs to the guidance's CloudFront.** A WAF rule group of ours, referenced first, blocks
  `/openrtb2/*` without the stage's origin secret (one of two values, for rotation).
- **The body-template rule.** The body carries only constants and MediaTailor's own variables (`session.uuid`,
  `session.avail_duration_secs`, `scte.event_id`), each inside a JSON string:
  `{"ext":{"prebid":{"storedrequest":{"id":"fast-pod-{{session.avail_duration_secs}}s"}}}}`. Values a player can set,
  including the playback ticket, travel only in header templates.
- **Stored requests name the own book only.** `fast-pod-{30,60,90,120}s` and the `fast-pod-kids-*` variants hold one
  pod of one to four 30 s slots for the own book, a `generic` alias on the Pod Decision Service. Third-party bidders,
  IDs and user data are added only by the fast-ctv policy hook, after a positive, in-time answer from the Pod Decision
  Service. A hook that is skipped, slow, failing or absent therefore leaves the own book and house ads: no
  third-party call.
- **One rendered configuration per stage** (`core/pbs_config.py`):
  - the guidance's five shipped adapters off, and its simulator on in demo mode only;
  - `server.http.server-instances` = vCPU;
  - the guidance's settings-cache defaults;
  - the stage's account, with the module's execution plan and Activity Controls that keep IDs, user data and precise
    geolocation from every bidder but the own book.

  The configuration is rendered without secrets.

## Consequences
- A second deployable app with its own toolchain (the guidance's Poetry project and a CDK bootstrap per stage),
  deployed and destroyed by repository scripts. Upstream releases are adopted by moving the pin after a review of the
  files `verify` reports.
- A JVM fleet beside the Python repository: Prebid Server Java on x86 Fargate (its jlinked runtime is
  architecture-specific), plus our module in Java.
- A stage with demand costs about $120–150 a month idle while deployed (NAT gateways, ALB, WAF, the Valkey cache
  minimum). Destroying it leaves the guidance's KMS keys pending deletion for 7 days.
- A break off the 30.03 s grid, or with a missing duration (MediaTailor sends 300 s), names a stored request that does
  not exist. Prebid Server answers 400, and the Channel Assembly slate plays.
- When AWS publishes the guidance as a construct library, the overlays become construct properties and the app can
  be composed.
