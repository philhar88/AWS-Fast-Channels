# Agentic selling: AdCP buyer agents book the own book

The `selling` module sells a stage's monetized channels over the Ad Context Protocol (AdCP). A buyer agent discovers
the stage's seller agent, reads its products, books a media buy, and the buy serves through the
[Pod Decision Service](pds.md) own book like any direct line: it is reserved per break, counted by our impression
tracker, and written to the ledger. The buyer reads the delivery back over AdCP.

The module is glue between pinned upstream agents:

| Role | What runs | Pin |
|---|---|---|
| Seller of record | The Prebid Sales Agent (`adcp-sales-agent` 2.0.0, AdCP 3.1.1), MCP on `/mcp/` and A2A 1.0 on `/a2a`, on Fargate (arm64) and RDS for PostgreSQL | `prebid/salesagent` `f92de9b` |
| Buyer agent | The AWS reference buyer's A2A runtime (Strands, Bedrock), on AgentCore Runtime | `guidance-for-advertising-agents-on-aws` `c86178b` |
| Governance agent | The AWS reference governance agent (AdCP campaign governance, MCP), on AgentCore Runtime | the same |
| Glue | A bridge container in the seller's task, and the sync function | this repository |

The module is a **sandbox**. The seller runs in its testing mode, every price is simulated and no money moves. Its
products, the buyer's brand and the CPM are demo values ([configuration](operations.md#configuration-reference)).

- [How a buy becomes a line](#how-a-buy-becomes-a-line)
- [The products](#the-products)
- [Creatives](#creatives)
- [Delivery back to the buyer](#delivery-back-to-the-buyer)
- [Deploy and destroy](#deploy-and-destroy)
- [Security](#security)
- [Cost](#cost)
- [Workarounds to remove](#workarounds-to-remove)
- [What it does not do](#what-it-does-not-do)

## How a buy becomes a line

```
buyer agent ──A2A 1.0──▶ seller (Prebid Sales Agent) ──▶ its database
     │                            ▲                          │
     └──MCP──▶ governance agent   │ delivery rows            ▼ every 60 s
                                  └──────── bridge ◀──▶ sync function ──▶ lines/booked.json ──▶ snapshot publisher
                                                                                                     │
                                         MediaTailor ◀── Prebid Server + PDS sidecar ◀── snapshot ◀───┘
```

1. **The journey.** The buyer agent calls the seller's `get_adcp_capabilities`, `sync_accounts` and `get_products`,
   syncs a plan to the governance agent (`sync_plans`), asks it to approve the buy (`check_governance`) and books it
   with the signed `governance_context` (`create_media_buy`). The seller books it on its mock ad server and answers
   `pending_creatives`.
2. **The bridge** (`vendor/agentic-selling/seller/fast/bridge.py`), a second container of the seller's task on the
   seller's own ORM, reads the tenant's media buys every minute and sends them to the sync function. The seller's
   database never leaves its VPC.
3. **The sync function** (`fast_channels.functions.selling.sync`) maps each served package to a line
   (`fast_channels.core.selling.booking`):

   | Line field | From the buy |
   |---|---|
   | `id` | `adcp-<media buy>-<n>` for its `n`-th package |
   | `advertiser`, `adomain` | the buy's advertiser and its brand's domain |
   | `budget`, `cpm` | the package's budget and fixed CPM |
   | `starts`, `ends` | the buy's flight |
   | `channels` | the channel of the package's product |
   | `creatives` | a configured demo creative ([below](#creatives)) |

   When the lines change it writes them to the PDS config bucket (`lines/booked.json`) and runs the snapshot
   publisher, which adds them to the deployment's lines and gives each its budget shards. The sidecars read the new
   snapshot within 30 seconds.
4. **Serving.** The own book bids a line inside its flight only, and `/select` leaves a line out of the breaks of
   other channels than its product's (outcome `off_target`). From there the line is like any direct line: a
   reservation per placed ad, delivery from our tracker's `BEACON_FIRED`, and money events in the ledger chain
   `pds/line/<line>` ([pds.md](pds.md#reservations-and-delivery)).

A buy is served while it is committed (`pending_creatives`, `pending_start`, `active`, ...), not paused and before its
end. A canceled, rejected, paused or completed buy loses its line at the next sync; what it reserved stays until the
sweeper releases it. A package without a fixed USD CPM, a budget, a known product, a brand domain or a free creative
gets no line, and the bridge logs why (`unserved`).

## The products

One non-guaranteed product per monetized channel (`modules.release.channels`), at `modules.selling.cpm`:

- `<channel>-midroll`, "30 s mid-roll", in the AdCP reference format `video_ctv_midroll_30s`, CTV, instream;
- its description names the channel's breaks (the default ladder's 30, 60, 90 and 120 s, sold in whole 30 s slots)
  and the genres of the catalog's titles, which the sync function reads from the catalog table each time the bridge
  refreshes the products (every 15 minutes);
- delivery reporting of impressions and spend, hourly and daily.

AdCP 3.1.1 cannot describe a FAST channel as a channel, and the Prebid Sales Agent keeps no collections or
installments (W-8 A1 and A3), so the genres and breaks are in the description and the
placement rather than in collections.

## Creatives

The AWS reference buyer has no creative tasks: it never calls `sync_creatives`, and its `create_media_buy` carries
none. So a booked buy stays `pending_creatives` at the seller, and the own book serves it with one of the ladder
kit's direct demo creatives (`modules.selling.creatives`, `direct1` and `direct2`), held by its line while the buy
runs. A stage serves at most as many booked packages at once as it lists creatives, and none of them may be a demo
line's. The creatives exist only in `modules.demand.mode: demo`, which the module therefore needs.

A buyer's own creative also needs the reference creative agent beside the seller (for `preview_creative`) and a
creative readiness pipeline that preconditions it for MediaTailor. Neither is built.

## Delivery back to the buyer

The seller answers `get_media_buy_delivery` from its ad server adapter. The bridge writes each buy's delivery, our
tracker's impressions of its lines and what they cost (the budget shards' `delivered` amounts), into the mock
adapter's seeded-delivery rows, which the adapter returns verbatim in testing mode. So the buyer reads, over AdCP,
the same seller-side server count the PDS bills by: never the mock's simulation, and not an accredited measurement.
It is at most a minute behind the delivery function.

The seller reports each package under its own fallback id (`pkg_<product>_<n>`, not the booked package id), so a buy
with several packages shows their total split evenly (a W-8 A10 defect).

## Deploy and destroy

The module needs `pds` and `release` (and so `ops` and `demand`), `demand.mode: demo`, and at least one monetized
channel. Its stack, `FastChannels-<prefix>-Selling`, is deployed beside the guidance:

```bash
make deploy STAGE=fastcisell PREFIX=<throwaway>          # the repository's stacks; DemandBuild gets the images' repositories
make deploy-demand STAGE=fastcisell PREFIX=<throwaway>   # ... the guidance, Demand, Pds, Release, then Selling
uv run pytest tests/e2e/test_21_selling.py -m e2e --prefix <throwaway> -v
make destroy-demand STAGE=fastcisell PREFIX=<throwaway> && make destroy STAGE=fastcisell PREFIX=<throwaway>
```

`deploy-demand` builds the module's three images on the stage's arm64 CodeBuild project (AgentCore Runtime runs arm64
only): each upstream repository is cloned at its pinned commit (`vendor/agentic-selling/PIN.toml`) and built with its
own Dockerfile, unmodified; the seller gets the bridge as a layer that adds files and the AWS SDK. A rebuild is
skipped when the repository already has the context's digest as a tag. It then deploys the Selling stack with the
images, the catalog table and CloudFront's origin-facing prefix list. `destroy-demand` deletes the Selling stack first,
and its inventory reports anything tagged `fast:module=selling` that is left, the agents' log groups and any database
snapshot.

| Part | What it is |
|---|---|
| Seller | Fargate service `<prefix>-seller` (1 task, 1 vCPU, 2 GB, arm64) in the guidance VPC's private subnets: the seller and the bridge. RDS for PostgreSQL 17 `<prefix>-seller` (`db.t4g.micro`, 20 GB, one Availability Zone, no backups, no final snapshot). An internal load balancer, reached by CloudFront through a VPC origin, so the seller has HTTPS on `*.cloudfront.net` and no public address. Secrets Manager: the database password, a seed from which the container derives the seller's own keys, and the buyer's bearer token |
| Governance agent | AgentCore runtime `<prefix>_governance` (MCP), Cognito JWT inbound (a client-credentials client); its state table `<prefix>-governance-state` and an ES256 KMS key `alias/<prefix>-governance-signing` for its `governance_context` tokens |
| Buyer agent | AgentCore runtime `<prefix>_buyer` (A2A), SigV4 inbound; its sessions table `<prefix>-buyer-sessions`; its model `modules.selling.buyer_model` |
| Sync function | `fast_channels.functions.selling.sync`, invoked by the bridge; reads the catalog table and the lines' budget shards, writes `lines/booked.json` and runs the publisher |

## Security

- **Inbound.** The seller is public, as an AdCP seller must be: discovery is unauthenticated and every other task
  needs a principal's bearer token. The buyer agent takes SigV4 only (principals of the account). The governance
  agent takes only its client's Cognito tokens.
- **Outbound.** The seller dials nothing outside AWS: its creative agent is fenced to a host that cannot resolve
  ([W-8 A9](#workarounds-to-remove)), its AI features are off, and its cron jobs do not run. The buyer dials the
  seller, the governance agent, Cognito and Bedrock.
- **The buyer's credentials** (the seller's bearer token and the governance client's secret) are in its runtime's
  environment, because the reference buyer reads them from nowhere else: whoever may call
  `bedrock-agentcore:GetAgentRuntime` can read them.
- **Governance tokens are not verified.** The seller accepts and ignores `governance_context`; verifying it needs
  seller-side support that the Prebid Sales Agent does not have yet (W-8 A2).

## Cost

TODO(e2e): measured idle and per-run cost.

## Workarounds to remove

Each is marked `OPTIMIZATION(aws): W-10 ...` (the AWS guidance) or `OPTIMIZATION(oss): W-8 ...` (the Prebid Sales
Agent) where it lives, and listed with the repository's other workarounds in [workarounds.md](workarounds.md). Remove it when the upstream fix ships.

| Id | Upstream behaviour | Our workaround | Where | When it is fixed |
|---|---|---|---|---|
| `W-10` item 1 | The reference buyer's `create_media_buy` refuses without an approved `check_governance`, even with no governance agent | The governance agent is deployed with the buyer | `infra/.../selling/agents.py` | Make the governance agent optional |
| `W-10` item 2 | The reference buyer's A2A runtime fails every turn without a sessions table | The module creates `<prefix>-buyer-sessions` | `infra/.../selling/agents.py` | Drop the table |
| `W-10` items 3 and 4 | `deploy_all.py` escapes a permissions boundary, and the agents are not published as images | The stage builds the pinned upstream code and deploys the agents with its own CDK | `infra/.../selling/agents.py`, `infra/.../selling/image_build.py`, `vendor/agentic-selling` | Deploy the published images or constructs |
| `W-10` item 6 | The reference buyer has no creative tasks | Booked buys serve the kit's demo creatives and stay `pending_creatives` | `core/.../selling/booking.py` | Serve the buyer's creatives (with a readiness pipeline) |
| `W-8` A7 | Single-tenant mode blocks `create_media_buy` until SSO is set up | `ADCP_MULTI_TENANT=true` | `infra/.../selling/seller.py` | Single-tenant |
| `W-8` A8 | The seller rebuilds `@target-uri` from `X-Forwarded-Proto`, which the load balancer sets to `http` | CloudFront sends `Fly-Forwarded-Proto: https` | `infra/.../selling/seller.py` | Drop the header |
| `W-8` A9 | Testing mode still dials the public reference creative agent | `CREATIVE_AGENT_URL` names a host that cannot resolve | `infra/.../selling/seller.py` | Drop the setting |
| `W-8` A10 | Products authorized by property tags publish the tenant's host as their publisher domain | The bridge names the publisher domain in each product's properties | `vendor/.../fast/bridge.py` (`upsert_products`) | Authorize by tag |
| `W-8` A11 | No ad server adapter for MediaTailor and the PDS | The bridge reads buys from the seller's database, and writes delivery to the mock adapter's seeded-delivery rows | `vendor/.../fast/bridge.py` | A `mediatailor_pds` adapter replaces the bridge |
| `W-8` A12 | The seller does not percent-decode the password in `DATABASE_URL`, so a password that needs encoding fails to log in and the seller exits at boot | The generated database password is letters, digits and `-._` only | `infra/.../selling/seller.py` (`PASSWORD_EXCLUDE`) | Allow any password characters |

## What it does not do

- **Real money or certification.** The seller is a testing-mode sandbox. Listing it with a storefront, or AdCP
  certification, is an owner's decision (W-8 part B).
- **Buyer creatives** ([above](#creatives)).
- **Guaranteed products, forecasts, targeting beyond the channel and flight.** Products are non-guaranteed, a line
  airs on its product's channel only and caps nothing per session.
- **Signed requests.** The buyer authenticates with a bearer token; the seller's RFC 9421 posture is advertised but
  not required.
- **Scale.** One seller task and a micro database: a demonstration, not a sales platform.
