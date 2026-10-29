# 0022 — Delivery states, counterparty billing, and the hash-chained ledger

**Status:** Proposed. This draft covers the ledger. The delivery states and counterparty billing are added when the
Pod Decision Service's own book lands, and the record is accepted then.

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
- Evidence: on `fastm`, 1,000 synthetic records over 4 chains were appended, chained, archived and verified in
  127 s. Archived objects carry `GOVERNANCE` retention, and deleting a version is denied. The unit tests flag every
  single modification, drop, swap and insertion. Random interleavings of two writers, redeliveries and failures always
  verify.
