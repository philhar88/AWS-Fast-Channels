# 0022 — Delivery states, counterparty billing, and the hash-chained ledger

**Status:** Accepted (the ledger with the ops module, the delivery states and counterparty billing with the Pod
Decision Service's own book).

## Context

The monetization modules take actions that someone must be able to audit later:
- the Release Controller promotes and rolls back playback-configuration releases unattended;
- the Pod Decision Service reserves and releases budget for direct lines;
- runbooks act on channels.

An audit trail of such actions must show who did what, when and to what. It must also show that no record was
changed, removed, inserted or reordered afterwards, including by the stage's own operators.

Amazon QLDB, AWS's ledger database, is discontinued. S3 Object Lock keeps objects from being overwritten or deleted
for a retention period, but it does not order records or show that one is missing. DynamoDB conditional writes keep
an item from being overwritten, but an administrator can still delete one.

The own book sells direct lines with budgets. A pod is decided before anyone knows whether its ads will play:
MediaTailor bills the fills it plans, a viewer may leave mid-break, an ad may be skipped because MediaTailor has not
transcoded it yet. Its beacons, our only evidence of an impression, may arrive late (it retries them for up to an
hour) or more than once. The buyer's own count, not ours, is what an insertion order bills (IAB/4A's terms).

## Decision

- **Append.** Producers append entries to a DynamoDB table, `<prefix>-ledger`, with a conditional put
  (`attribute_not_exists`). Their IAM grant is `PutItem` only (`LedgerStorage.grant_append`).
- **Chain.** A Lambda on the table's stream links each entry into its chain, one chain per `chain_id`
  (`fast_channels.core.ledger`). Each record gets a sequence number from 1 and the SHA-256 of its predecessor. The
  hash covers a canonical encoding: sorted JSON, no whitespace, no floats, millisecond UTC times. The chain's head and
  the linked record are written in one conditional transaction, so concurrent writers cannot fork a chain and a
  redelivered entry is linked once.
- **Archive.** The linked records go through Amazon Data Firehose to an S3 bucket with Object Lock:
  - governance mode by default; compliance mode only in production, after the retention review;
  - a default retention of `modules.ops.ledger_retention_days` (30; 1 on development stages);
  - the bucket is retained when its stack is deleted.
- **Verify.** `scripts/ledger_verify.py verify` recomputes every chain from the archive and compares each chain's end
  with its head in the table. Modified, dropped, inserted, duplicated, reordered and truncated records are all
  findings.
- **Detect tampering.** An entry that is changed or removed in the table after it was appended raises the
  `<prefix>-ledger-entries-changed` alarm; its first version is already chained and archived.
- Records never hold a viewer id.

### Delivery states and billing

- **States.** An own-book line ad that `/select` places goes through `RESERVED`, `DELIVERED` (our tracker's
  impression in MediaTailor's `BEACON_FIRED` log event), `RELEASED` (no impression two hours after the reservation)
  and later `RECONCILED`. A reservation is never released by DynamoDB's TTL, which only cleans up; an impression
  that arrives after its release is delivered anyway, marked late, and its price is taken again.
- **Liability.** A reservation is one `TransactWriteItems`: the price (`cpm / 1000`) leaves one of the line's
  4 budget shards, whose `remaining` never goes below zero; the session's cap count goes up; the reservation is
  written; the ledger gets the money event. So a line's liability (reserved plus delivered) never exceeds its budget,
  and a reservation without its ledger entry cannot exist. A repeated `/select` of the same break reserves nothing
  more: a reservation is keyed by the break (`<sid>.<evid>`) and the creative.
- **Counting.** Fills count once per `(sid, evid)` and impressions once per `(sid, evid, crid)`, from
  `beaconInfo.beaconUri` (not CloudFront's logs), and only for our tracker fired in the session it names.
- **Money events.** Every change of a line's money is a ledger entry in its chain `pds/line/<id>`: `money.budget`,
  `money.reserve`, `money.deliver` and `money.release`, amounts as decimal strings.
- **Billing.** Our count is a seller-side server count, reported as such, not as an accredited measurement. The
  counterparty's count is what bills; reconciliation (`RECONCILED`) compares the two, and a large gap goes to a
  person.

## Consequences

- About 1,100 lines of our own code (the core module, the writer and producers' client, the constructs, the
  verifier), with no new service to run. The
  idle cost is near zero: on-demand DynamoDB, Firehose per GB, S3.
- `make destroy` leaves the archive behind. With governance mode, `scripts/ledger_verify.py purge --apply
  --bypass-governance` (it needs `s3:BypassGovernanceRetention`) deletes a destroyed stage's archive at once. Without
  that permission, and always in compliance mode, it deletes the archive only after the retention ends.
- The chain proves the order and completeness of what the writer archived. It cannot prove that a producer appended
  what it should have, and a principal with `dynamodb:PutItem` can append false entries (they are attributed to the
  principal recorded in them, which the producer chooses).
- A line can under-deliver near the end of its budget: a break reserves from one shard, so a break can be refused
  while another shard still has budget, and each shard can keep less than one impression's price unspent (under
  4 impressions' worth per line). An undelivered reservation holds its price for two hours, and a session's cap
  count is never given back.
- Delivery costs a few DynamoDB writes per impression and one Lambda invocation per log batch; reservations one
  transaction per break with line ads.
- Evidence (delivery): against DynamoDB Local, 300 breaks racing for a $1 line at $6 CPM held exactly 164
  reservations (41 per $0.25 shard), left $0.016 unreserved and wrote 164 `money.reserve` entries; a repeated
  reservation, a redelivered beacon batch and a release raced by a late impression all keep the totals. The delivery
  function turns recorded `BEACON_FIRED` shapes into `DELIVERED` with the dedupe keys.
- Evidence (ledger): on `fastm`, 1,000 synthetic records over 4 chains were appended, chained, archived and verified in
  127 s. Archived objects carry `GOVERNANCE` retention, and deleting a version is denied. The unit tests flag every
  single modification, drop, swap and insertion. Random interleavings of two writers, redeliveries and failures always
  verify.
