# 0020 — Programmatic demand through the AWS Prebid Server guidance

**Status:** Proposed (amends [0001](0001-python-cdk-and-uv-workspace.md) and
[0002](0002-l2-constructs-over-solutions-constructs.md), exception A). It is accepted when the demand and Pod Decision
Service modules pass their end-to-end legs. Its placement of the Pod Decision Service is superseded, and its
deployment of the guidance amended (a wrapper of the guidance's app adds the sidecar), by
[0036](0036-pds-sidecar-in-the-prebid-server-task.md). Amended on 2026-10-30: Prebid Server Java 4.4.0 is the only
supported release, and `modules.demand.pbs_version` is gone (see "The Prebid Server Java release" below).

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
- Prebid Server Java 3.43.0, the guidance's release, has no model for the OpenRTB 2.6 content fields `genres`, `gtax`,
  `realtime`, `firstbroadcast` and `data[].cids`, and drops them without a warning before any hook or bidder sees
  them. Release 4.4.0 (October 2026) models them. It needs Java 25 (Spring Boot 4, Vert.x 5), and two settings 3.43.0
  does not have: `gdpr.vendorlist.live.startup-cache-dir`, and `meta-info.vendor-id` on every configured `generic`
  alias (without it, startup fails with a `NullPointerException`).
- The guidance's log analytics reporter (`EnableLogAnalytics`) logs every request's headers verbatim, the playback
  ticket and the origin secret included.

## Decision
- **The guidance is deployed as its own CDK app**, not composed into this repository's app (exception A to
  [0002](0002-l2-constructs-over-solutions-constructs.md)). It runs at a pinned tag and commit
  (`vendor/prebid-guidance/PIN.toml`, with the SHA-256 of every upstream file the overlays patch or rely on), with
  these overlays:

  | Overlay | Change |
  |---|---|
  | O1 | Stack-name prefix and a dedicated CDK bootstrap per stage, from CDK context; the prefix also names the
  MetricsEtl Glue database, the guidance's only account-unique name that ignored the stack |
  | O2 | Stack parameter `ExtraWafRuleGroupArn`: a customer rule group referenced first by the guidance's web ACL |
  | O4 | The fast-ctv module compiled through the guidance's `amt-bidder` injection point |
  | O5 | Bidder simulator creatives from configuration (30.03 s HLS creatives preconditioned on the ladder) |
  | O6 | The JDK of the Prebid Server Java build is a build argument (Corretto 21 as shipped, 25 for 4.x) |
  | O7 | The Prebid Server service deploys at 100% minimum healthy and 200% maximum |

  `scripts/prebid_guidance.py` checks the pinned commit out, refuses it if the tag moved, a pinned file changed or a
  patch does not apply, then applies the overlays. The repository reads only the parameters the deploy script writes
  under `/<prefix>/demand/`. CI applies the overlays to the pinned tag on every run.
- **The guidance deploys onto a CDK bootstrap of its own** per stage (qualifier = the prefix), never onto the
  account's shared `hnb659fds` bootstrap, whose CloudFormation execution role is often AdministratorAccess. Its
  execution policy (`vendor/prebid-guidance/bootstrap-policy.json`) allows the guidance's services, S3, Lambda and IAM
  roles only on the guidance's names, managed-policy attachment only for the two Lambda execution policies it uses,
  and KMS without data access. Inline role policies stay unconstrained; that is the residual risk.
- **The image** is the guidance's Dockerfile with the overlays and the module, built by an x86 CodeBuild project of
  the stage and tagged by the content of its build context.
- **The Prebid Server Java release is 4.4.0, the only one supported** (`core.pbs_config.PBS_VERSION`). It was a
  stage setting, `modules.demand.pbs_version`, with the guidance's 3.43.0 as the alternative. The amendment of
  2026-10-30 removed 3.43.0 and the setting with it: 3.43.0 can carry the OpenRTB 2.6 content fields only in
  `app.content.ext`, and keeping it meant a second carriage, a second CI build and a second contract run. 4.4.0 was
  the latest Prebid Server Java release on that date. The guidance builds 3.43.0, so `PIN.toml` names 4.4.0's commit,
  JDK and Maven options. The image build gives the Dockerfile that release's `docker-build-config.json` and the JDK
  (O6), and O4 replaces the guidance's `amt` adapter configuration with a 4.x port. The rendered configuration has
  4.x's two required settings; the own book, a configuration-level alias, has vendor id 0 (it is the publisher, not
  an IAB TCF vendor). CI builds and tests the module and the contract on 4.4.0. A later release is adopted by moving
  `PBS_VERSION` and `PIN.toml` together, after the same tests pass on it.
- **The guidance's log analytics stays off** (`EnableLogAnalytics=false`) while its reporter logs request headers.
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
- **The Pod Decision Service runs in the Prebid Server VPC** (superseded by
  [0036](0036-pds-sidecar-in-the-prebid-server-task.md): a sidecar in the Prebid Server task), behind an internal
  Application Load Balancer at
  `pds.<prefix>.fast.internal` that admits only the Prebid Server tasks' security group. Its functions run outside
  the VPC (the load balancer invokes them), and a census-gated provisioned concurrency keeps them warm while channels
  run. The reason is the 50 ms `/policy` budget: in a live test over API Gateway and a NAT gateway, a third of
  MediaTailor's auctions fell back to the own book, the first break of every session among them, with cold functions
  and connections. A Fargate service is built only if a measured break herd needs it. Each function serves from a
  snapshot in a private config bucket (the demand allow-list, CloudFront's origin-facing networks), which a publisher
  refreshes, and logs one decision record per call into the lake.

## Consequences
- A second deployable app with its own toolchain (the guidance's Poetry project and a CDK bootstrap per stage),
  deployed and destroyed by repository scripts. Upstream releases are adopted by moving the pin after a review of the
  files `verify` reports.
- A JVM fleet beside the Python repository: Prebid Server Java on x86 Fargate (its jlinked runtime is
  architecture-specific), plus our module in Java.
- A stage with demand costs about $120–150 a month idle while deployed (NAT gateways, ALB, WAF, the Valkey cache
  minimum). Destroying it leaves the guidance's KMS keys pending deletion for 7 days.
- The OpenRTB 2.6 content fields reach bidders first-class, so the Pod Decision Service has one carriage. There is
  no rollback to 3.43.0: a stage that needs it would need the `app.content.ext` carriage back.
- A break off the 30.03 s grid, or with a missing duration (MediaTailor sends 300 s), names a stored request that does
  not exist. Prebid Server answers 400, and the Channel Assembly slate plays.
- When AWS publishes the guidance as a construct library, the overlays become construct properties and the app can
  be composed.
