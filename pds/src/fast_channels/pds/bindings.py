"""The ticket lookup on every ``/policy`` call (identity module, ADR-0018, DESIGN-v2.2 §5.4).

The module forwards the session's playback ticket as ``t`` (``X-Fast-T``). With ``IDENTITY_TABLE`` set the sidecar
reads ``T#<t>`` from it with a consistent read, on the reservations' client (100 ms, one attempt), and decides the
break with ``core.identity.binding``: bound to this session and configuration inside the lease, or anonymous with the
reason. It writes only what the check asks for: the self-redeem of a fresh ticket whose PRE_SESSION redeem never
landed, and ``last_seen`` at most every 10 minutes.

A ticket bound to a signed-in viewer then needs their consent: the row ``C#<vid>`` of the same table
(``core.identity.consent``), read consistently and kept :data:`CONSENT_FRESH_S` seconds, so a change reaches the next
break within that. A read that fails keeps a kept answer up to :data:`CONSENT_STALE_S` seconds old; after that, and for
a missing, malformed or stale row, the break is SAFE. A guest's ticket carries its mode and needs no read.

A missing or malformed ticket, a probe and a kids configuration make no call. A lookup that fails (a throttle, a
timeout) makes the break SAFE: it may be a signed-in viewer's, so no third party is asked
(``core.identity.binding.LOOKUP_FAILED``). Counts go to the sidecar's metrics: ``BindingLookups`` by reason,
``BindingErrors``, ``BindingSelfRedeems``, ``BindingReplays``, ``ConsentLookups`` by consent and ``ConsentErrors``.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

from fast_channels.core.identity import consent as consents
from fast_channels.core.identity.binding import (
    LOOKUP_FAILED,
    Action,
    Binding,
    PolicyInputs,
    check,
    policy_inputs,
    refresh,
    self_redeem,
)
from fast_channels.core.identity.consent import UNKNOWN, ConsentRecord, StoredConsent
from fast_channels.core.identity.ticket import key
from fast_channels.core.pds.models import Reason
from fast_channels.core.pds.policy import is_kids_configuration, ticket_status
from fast_channels.functions.settings import settings
from fast_channels.pds import state
from fast_channels.pds.telemetry import counters

if TYPE_CHECKING:
    from fast_channels.core.pds.models import PolicyRequest

logger = Logger(child=True)
_DESERIALIZE: Final = TypeDeserializer()
CONDITION_FAILED: Final = "ConditionalCheckFailedException"
CONSENT_FRESH_S: Final = 5.0
"""A viewer's consent is read again after this long (DESIGN-v2.2 §5.4: an opt-out reaches the next break in 5 s)."""
CONSENT_STALE_S: Final = 60.0
"""A kept consent older than this is not used when the store cannot be read: the break is SAFE (§5.5)."""
CONSENT_KEPT_MAX: Final = 50_000
"""Viewers whose consent is kept at most; past it the oldest half is dropped."""


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class Bindings:
    """The lookup (module docstring) on ``client`` against ``table``; ``clock`` gives epoch milliseconds."""

    def __init__(self, table: str, client: Any, *, clock: Callable[[], int] = _now_ms) -> None:
        self.table, self.client, self.clock = table, client, clock
        self.kept: dict[str, tuple[int, StoredConsent]] = {}
        """Each viewer's last consent answer and when it was read (epoch ms)."""

    def inputs(self, request: PolicyRequest) -> PolicyInputs:
        """The policy's identity, viewer and consent for ``request``."""
        if (status := ticket_status(request.t)) is not None:
            return PolicyInputs(status)
        if request.probe or is_kids_configuration(request.pc):
            return PolicyInputs(Reason.UNKNOWN)  # the mode is SAFE or KIDS whatever the binding says
        try:
            binding = self._binding(request)
        except (BotoCoreError, ClientError) as error:
            logger.warning(
                "the ticket's binding could not be read",
                extra={"error": type(error).__name__, "correlation_id": request.decision_id},
            )
            counters.add("BindingErrors")
            return LOOKUP_FAILED
        counters.add("BindingLookups", dimension=("reason", binding.reason.value))
        if binding.reason is Reason.REPLAY:
            counters.add("BindingReplays")
        if not binding.signed_in or binding.vid is None:
            return policy_inputs(binding)
        consent = self.consent(binding.vid, decision_id=request.decision_id)
        counters.add("ConsentLookups", dimension=("consent", consent.consent.value))
        return policy_inputs(binding, consent)

    def consent(self, vid: str, *, decision_id: str = "") -> StoredConsent:
        """``vid``'s consent: the kept answer while fresh, else the row's (module docstring)."""
        now = self.clock()
        kept = self.kept.get(vid)
        if kept is not None and now - kept[0] < CONSENT_FRESH_S * 1000:
            return kept[1]
        try:
            answer = self.client.get_item(
                TableName=self.table, Key={"pk": {"S": consents.key(vid)}}, ConsistentRead=True
            )
        except (BotoCoreError, ClientError) as error:
            logger.warning(
                "the viewer's consent could not be read",
                extra={"error": type(error).__name__, "correlation_id": decision_id},
            )
            counters.add("ConsentErrors")
            return kept[1] if kept is not None and now - kept[0] < CONSENT_STALE_S * 1000 else UNKNOWN
        found = consents.stored(_record(answer.get("Item")), now=datetime.fromtimestamp(now / 1000, UTC))
        if len(self.kept) >= CONSENT_KEPT_MAX:
            for old in sorted(self.kept, key=lambda name: self.kept[name][0])[: CONSENT_KEPT_MAX // 2]:
                del self.kept[old]
        self.kept[vid] = (now, found)
        return found

    def _binding(self, request: PolicyRequest) -> Binding:
        answer = self.client.get_item(TableName=self.table, Key={"pk": {"S": key(request.t)}}, ConsistentRead=True)
        item = answer.get("Item")
        row = {name: _DESERIALIZE.deserialize(value) for name, value in item.items()} if item else None
        now = self.clock()
        binding = check(row, sid=request.sid, pc=request.pc, now_ms=now)
        match binding.action:
            case Action.SELF_REDEEM:
                try:
                    self.client.update_item(
                        **self_redeem(self.table, request.t, sid=request.sid, pc=request.pc, now_ms=now)
                    )
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") != CONDITION_FAILED:
                        raise
                    return Binding(Reason.EXPIRED_OR_RACED)  # another session or the clock got there first
                counters.add("BindingSelfRedeems")
            case Action.REFRESH:
                try:
                    self.client.update_item(**refresh(self.table, request.t, sid=request.sid, now_ms=now))
                except (BotoCoreError, ClientError) as error:  # the break stays bound; the next one tries again
                    logger.info("last_seen was not renewed", extra={"error": type(error).__name__})
        return binding


def _record(item: dict[str, Any] | None) -> ConsentRecord | None:
    """The consent row of a ``GetItem`` answer; a malformed one counts as missing."""
    if not item:
        return None
    try:
        return ConsentRecord.model_validate({name: _DESERIALIZE.deserialize(value) for name, value in item.items()})
    except ValueError:  # pydantic's ValidationError is a ValueError
        logger.warning("a consent row is malformed and counts as missing")
        return None


@cache
def bindings() -> Bindings | None:
    """The sidecar's lookup, or None without the identity module (``IDENTITY_TABLE`` unset)."""
    table = settings().identity_table
    return Bindings(table, state.dynamodb()) if table else None
