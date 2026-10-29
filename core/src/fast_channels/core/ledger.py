"""The ledger: append-only records of changes, money events and runbook actions, hash-chained per chain
(ADR-0022).

A producer (the Release Controller, the PDS, a runbook) appends a :class:`LedgerEntry` to the ``<prefix>-ledger``
table with a conditional put, so no entry is ever overwritten (:func:`entry_item`). The ledger writer reads the table's
stream and links each entry into its chain (:func:`link`): one chain per ``chain_id``, numbered from 1, each
:class:`ChainedRecord` carrying the SHA-256 of its predecessor, and the chain's :class:`ChainHead` recording the last
one. The linked records are archived in S3 under Object Lock, and :func:`verify` proves that a set of them, compared
with the heads, is complete, unmodified and in order.

The hash covers the canonical encoding (:func:`canonical`) of every field of the record except ``hash`` itself: JSON
with sorted keys, no whitespace and UTF-8, holding only strings, integers, booleans and nulls (amounts are decimal
strings), so that any implementation re-derives the same bytes. Records never hold a viewer id.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

GENESIS: Final = "0" * 64
"""``prev_hash`` of the first record of a chain."""
CHAIN_ID_PATTERN: Final = r"^[a-z0-9][a-z0-9._:/-]{0,127}$"
ULID_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
"""Crockford's base 32, the ULID alphabet."""
ULID_PATTERN: Final = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
ENTRY_KEY_PREFIX: Final = "E#"
"""Sort key prefix of entry items (``E#<record id>``)."""
LINK_KEY_PREFIX: Final = "L#"
"""Sort key prefix of the writer's linked-record items (``L#<record id>``)."""
HEAD_KEY: Final = "HEAD"
"""Sort key of a chain's head item."""
EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

type Json = str | int | bool | list[Json] | dict[str, Json] | None
"""What a ledger detail may hold: no floats, whose text form differs between implementations."""


class LedgerKind(StrEnum):
    """What a record is about."""

    RELEASE = "release"
    """A Release Controller step (validate, apply, smoke, promote, roll back)."""
    MONEY = "money"
    """A reservation, delivery, release or reconciliation of an own-book line (§13.4)."""
    RUNBOOK = "runbook"
    """A runbook action (§18.8)."""
    ENVELOPE = "envelope"
    """A new version of the autonomy envelope (§19.3)."""
    TEST = "test"
    """Synthetic records written to check the ledger (``scripts/ledger_verify.py --write``)."""


def _utc_millis(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError("ledger times must be timezone-aware")
    return at.astimezone(UTC).replace(microsecond=at.microsecond // 1000 * 1000)


def _iso_millis(at: datetime) -> str:
    return f"{at:%Y-%m-%dT%H:%M:%S}.{at.microsecond // 1000:03d}Z"


class LedgerEntry(BaseModel):
    """One fact a producer appends; never changed afterwards."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    chain_id: str = Field(pattern=CHAIN_ID_PATTERN, description="Its chain, e.g. release/fastm-demo or runbook/r13.")
    record_id: str = Field(pattern=ULID_PATTERN, description="ULID (new_record_id), unique within the chain.")
    kind: LedgerKind
    at: Annotated[datetime, AfterValidator(_utc_millis)] = Field(description="When it happened (UTC, milliseconds).")
    principal: str = Field(
        min_length=1, max_length=256, description="Who acted: a role, a machine principal, a person."
    )
    action: str = Field(pattern=r"^[a-z][a-z0-9_.]{0,63}$", description="What was done, e.g. release.promote.")
    subject: str = Field(
        min_length=1, max_length=512, description="What it was done to, e.g. a playback configuration."
    )
    correlation_id: str | None = Field(default=None, max_length=256, description="change_id, execution name, ...")
    detail: dict[str, Json] = Field(default_factory=dict, description="Facts of the record; no floats (see Json).")


class ChainHead(BaseModel):
    """The last record of a chain, as the writer recorded it."""

    model_config = ConfigDict(frozen=True)

    chain_id: str
    seq: int = Field(ge=1)
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_id: str


class ChainedRecord(LedgerEntry):
    """A :class:`LedgerEntry` linked into its chain."""

    seq: int = Field(ge=1, description="Position in the chain, from 1, without gaps.")
    prev_hash: str = Field(pattern=r"^[0-9a-f]{64}$", description="hash of the record before; GENESIS for seq 1.")
    hash: str = Field(pattern=r"^[0-9a-f]{64}$", description="SHA-256 of canonical(record): every field but this one.")

    @property
    def head(self) -> ChainHead:
        """The chain's head once this record is linked."""
        return ChainHead(chain_id=self.chain_id, seq=self.seq, hash=self.hash, record_id=self.record_id)

    def line(self) -> str:
        """The record as one line of the archive (JSON, then a newline)."""
        return self.model_dump_json() + "\n"


def canonical(record: LedgerEntry) -> bytes:
    """The bytes a record's hash covers: every field but ``hash``, as JSON with sorted keys and no whitespace, with
    ``at`` as ``YYYY-MM-DDTHH:MM:SS.mmmZ``."""
    data = record.model_dump(mode="json", exclude={"hash"})
    data["at"] = _iso_millis(record.at)
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(record: ChainedRecord) -> str:
    """The SHA-256 (hex) a record's ``hash`` must equal."""
    return hashlib.sha256(canonical(record)).hexdigest()


def link(entry: LedgerEntry, head: ChainHead | None) -> ChainedRecord:
    """``entry`` as the record after ``head`` in its chain (``None``: the chain's first record).

    Raises:
        ValueError: ``head`` belongs to another chain.
    """
    if head is not None and head.chain_id != entry.chain_id:
        raise ValueError(f"{entry.record_id} is in chain {entry.chain_id}, not {head.chain_id}")
    draft = ChainedRecord(
        **entry.model_dump(),
        seq=1 if head is None else head.seq + 1,
        prev_hash=GENESIS if head is None else head.hash,
        hash=GENESIS,
    )
    return draft.model_copy(update={"hash": digest(draft)})


def new_record_id(at: datetime, entropy: bytes) -> str:
    """A ULID: ``at`` in milliseconds since the epoch (48 bits), then 80 bits of ``entropy`` (10 bytes, e.g.
    ``os.urandom(10)``), in Crockford base 32. Ids sort by time.

    Raises:
        ValueError: ``entropy`` is not 10 bytes, or ``at`` is naive or before the epoch.
    """
    if len(entropy) != 10:
        raise ValueError("a ULID takes 10 bytes of entropy")
    if at.tzinfo is None or at < EPOCH:
        raise ValueError("ULID times are timezone-aware and after the epoch")
    value = (at - EPOCH) // timedelta(milliseconds=1) << 80 | int.from_bytes(entropy)
    return "".join(ULID_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))


def entry_item(entry: LedgerEntry) -> dict[str, str]:
    """The table item of ``entry``: ``pk`` = chain, ``sk`` = ``E#<record id>``, ``type`` = ``entry``, every value a
    string (``detail`` as JSON), so no number goes through DynamoDB's decimal conversion."""
    item = {
        "pk": entry.chain_id,
        "sk": f"{ENTRY_KEY_PREFIX}{entry.record_id}",
        "type": "entry",
        "kind": entry.kind.value,
        "at": _iso_millis(entry.at),
        "principal": entry.principal,
        "action": entry.action,
        "subject": entry.subject,
        "detail": json.dumps(entry.detail, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
    }
    if entry.correlation_id is not None:
        item["correlation_id"] = entry.correlation_id
    return item


def entry_from_item(item: Mapping[str, str]) -> LedgerEntry:
    """The entry an :func:`entry_item` holds.

    Raises:
        pydantic.ValidationError: the item is not a well-formed entry.
        ValueError: it is not an entry item.
    """
    if item.get("type") != "entry" or not item.get("sk", "").startswith(ENTRY_KEY_PREFIX):
        raise ValueError(f"not a ledger entry item: {item.get('pk')}/{item.get('sk')}")
    return LedgerEntry(
        chain_id=item["pk"],
        record_id=item["sk"].removeprefix(ENTRY_KEY_PREFIX),
        kind=LedgerKind(item["kind"]),
        at=datetime.fromisoformat(item["at"]),
        principal=item["principal"],
        action=item["action"],
        subject=item["subject"],
        correlation_id=item.get("correlation_id"),
        detail=json.loads(item.get("detail") or "{}"),
    )


class Problem(StrEnum):
    """Why a chain fails verification."""

    MODIFIED = "modified"
    """A record's hash does not match its content (it was changed, or its position was)."""
    BROKEN_LINK = "broken_link"
    """A record's prev_hash is not its predecessor's hash: a record before it was replaced."""
    GAP = "gap"
    """The sequence skips: records are missing."""
    DUPLICATE = "duplicate"
    """Two different records claim one position."""
    BAD_START = "bad_start"
    """The chain does not start with seq 1 and GENESIS: its first records are missing."""
    TRUNCATED = "truncated"
    """The chain ends before its head, or its last record is not the head: records at its end are missing."""
    NO_RECORDS = "no_records"
    """The head names a chain of which no record was found."""


class Finding(BaseModel):
    """One problem at one position of a chain."""

    model_config = ConfigDict(frozen=True)

    chain_id: str
    seq: int
    record_id: str
    problem: Problem


class Verification(BaseModel):
    """The result of :func:`verify`."""

    model_config = ConfigDict(frozen=True)

    chains: dict[str, int] = Field(description="Distinct records per chain.")
    findings: list[Finding]

    @property
    def ok(self) -> bool:
        """No finding."""
        return not self.findings


def verify(records: Iterable[ChainedRecord], heads: Mapping[str, ChainHead] | None = None) -> Verification:
    """Check every chain of ``records`` (in any order; byte-identical copies count once): each hash recomputes,
    sequence numbers run from 1 without gaps or duplicates, each record names its predecessor's hash and, where
    ``heads`` has the chain, the chain ends at its head. A chain in ``heads`` without records is a finding too.

    Only byte-identical copies are merged (the writer may archive a record twice): a changed copy beside the
    original is reported as both a duplicate and a modified record."""
    chains: dict[str, dict[tuple[int, str, str], ChainedRecord]] = defaultdict(dict)
    for record in records:
        chains[record.chain_id].setdefault((record.seq, record.record_id, record.model_dump_json()), record)
    findings: list[Finding] = []
    for chain_id in sorted(set(chains) | set(heads or {})):
        chain = [chains[chain_id][key] for key in sorted(chains[chain_id])]
        head = (heads or {}).get(chain_id)
        if not chain:
            assert head is not None
            findings.append(
                Finding(chain_id=chain_id, seq=head.seq, record_id=head.record_id, problem=Problem.NO_RECORDS)
            )
            continue
        findings += _chain_findings(chain, head)
    return Verification(chains={c: len(r) for c, r in sorted(chains.items()) if r}, findings=findings)


def _chain_findings(chain: list[ChainedRecord], head: ChainHead | None) -> list[Finding]:
    findings: list[Finding] = []

    def found(record: ChainedRecord, problem: Problem) -> None:
        findings.append(Finding(chain_id=record.chain_id, seq=record.seq, record_id=record.record_id, problem=problem))

    previous: ChainedRecord | None = None
    for record in chain:
        if digest(record) != record.hash:
            found(record, Problem.MODIFIED)
        match previous:
            case None if record.seq != 1 or record.prev_hash != GENESIS:
                found(record, Problem.BAD_START)
            case None:
                pass
            case _ if record.seq == previous.seq:
                found(record, Problem.DUPLICATE)
            case _ if record.seq != previous.seq + 1:
                found(record, Problem.GAP)
            case _ if record.prev_hash != previous.hash:
                found(record, Problem.BROKEN_LINK)
        previous = record
    last = chain[-1]
    if head is not None and (last.seq, last.hash) != (head.seq, head.hash):
        found(last, Problem.TRUNCATED)
    return findings
