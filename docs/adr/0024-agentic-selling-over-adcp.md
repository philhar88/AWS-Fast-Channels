# 0024 — Agentic selling over AdCP: the Prebid Sales Agent as seller of record, A2A on the seller

**Status:** Accepted on 2026-11-05 for v4.1.0 (slice D): the selling leg (`tests/e2e/test_21_selling.py`) passed on a
throwaway stage. Amends [0011](0011-runtime-over-harness.md) ("A2A integration is not in v3.0"): the seller agent
serves A2A 1.0 beside MCP, and the reference buyer reaches it over A2A. Adds exception B to
[0002](0002-l2-constructs-over-solutions-constructs.md).

## Context

The owner chose agentic selling (M7) as slice D of v4.1.0: a buyer agent discovers and books the stage's inventory
over the Ad Context Protocol (AdCP), and the booking serves through the [PDS own book](../pds.md#the-own-book). The
design (owner decisions Y3 and Y5) names the pieces: the Prebid Sales Agent as seller of record (AdCP 3.1.1, MCP and
A2A 1.0), the AWS reference buyer and governance agents from the Guidance for Advertising Agents on AWS, glue only.

Phase 0 (P0-13, October 2026) ran the seller on Fargate and RDS and found:

- the reference buyer could not book against a conformant seller (its A2A client spoke 0.3 only, its
  `sync_accounts` omitted `billing`), which our upstream fix (guidance PR #48, merged as `c86178b`) corrected;
- the buyer books only after an approved `check_governance`, so its governance agent is required;
- the guidance's deployer cannot run inside a stage's names and permissions boundary, so the agents must be deployed
  from pinned images by our own IaC;
- the seller has no ad-server adapter for MediaTailor, and its MCP `tools/list` is 7 MB, too large for a Gateway
  target.

On 2026-11-05 it also turned out that the reference buyer has no creative tasks at all (no `sync_creatives`), so a
buy it books carries no creative.

## Decision

- **Seller.** The Prebid Sales Agent at a pinned commit, built from its own Dockerfile, on Fargate (arm64) and RDS for
  PostgreSQL in the guidance's VPC, behind an internal load balancer that CloudFront reaches through a VPC origin.
  It runs in testing mode (a sandbox) and multi-tenant, with one tenant: the stage. Buyers reach it directly over MCP
  or A2A 1.0, never through an AgentCore Gateway.
- **Buyer and governance agents.** The guidance's buyer (A2A) and governance (MCP) agents at a pinned commit, built
  unmodified on the stage's arm64 CodeBuild project, and deployed on AgentCore Runtime by the repository's CDK
  (exception B of 0002). The buyer is invoked with SigV4; the governance agent takes the buyer's Cognito
  client-credentials tokens and signs with a KMS ES256 key.
- **Glue, no adapter.** A bridge container in the seller's task reads booked buys from the seller's database with the
  seller's own ORM, and a sync function maps them to own-book lines (budget, flight, CPM, brand, channel) that the
  snapshot publisher adds to the deployment's lines. Delivery goes back through the mock adapter's seeded-delivery
  rows. An upstream `mediatailor_pds` adapter would replace the bridge (W-8 A11).
- **Lines learn flights and channels.** A line may carry a flight (`starts`, `ends`), which the own book honours, and
  the channels it is sold on, which `/select` honours (`off_target`). Unset, the snapshot is as before.
- **Creatives.** A booked package serves one of the stage's configured direct demo creatives, held while its buy
  runs, until the buyer can send creatives and a readiness pipeline can precondition them.

## Consequences

- A second protocol surface on the seller (A2A 1.0) to secure; its card declares no security scheme (W-8 A10).
- A small Postgres database and a Fargate task while the module is deployed, and a third container image fleet to
  patch (seller, buyer, governance), pinned by commit and base digest.
- The buyer's credentials sit in its runtime's environment, because the reference buyer reads them from nowhere else.
- The governance token is not verified by the seller until the Prebid Sales Agent supports it (W-8 A2): governance
  here approves the buyer's own spend, not the seller's acceptance.
- Each workaround of an upstream issue is marked `OPTIMIZATION(<owner>): <item>` and listed in
  [selling.md](../selling.md#workarounds-to-remove), to remove when the fix ships.

## Evidence

- Phase 0: the seller on AWS, its storyboards, the buyer journey with the buyer's request shapes (P0-13).
- The selling leg on a throwaway stage: see the pull request that added the module (v4.1.0).
