"""Decision records: one JSON line per ``/policy`` and per ``/select`` call, and one per impression of our tracker,
joined on ``decision_id``.

The PDS logs them (Powertools JSON); a subscription delivers them to the lake. The select record keeps every bid at or
above the floor with what became of it, which is what forgone-revenue accounting and off-policy evaluation need.
Records never hold the ticket, the viewer's address or headers, or the catalog context: only their outcomes (the
mode and reason, the viewer-IP rule's outcome, the context's length).
"""

from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from fast_channels.core.pds.delivery import Credit  # noqa: TC001 - pydantic resolves field types at runtime
from fast_channels.core.pds.models import (
    SCHEMA_VERSION,
    DeviceType,
    Mode,
    Money,
    PodReason,
    PolicyRequest,
    Reason,
    SelectRequest,
)
from fast_channels.core.pds.pod import Outcome  # noqa: TC001 - pydantic resolves field types at runtime
from fast_channels.core.pds.xff import IpRule  # noqa: TC001 - pydantic resolves field types at runtime

if TYPE_CHECKING:
    from fast_channels.core.pds.pod import PodDecision
    from fast_channels.core.pds.policy import PolicyDecision

POLICY_RECORD: Final = "pds.policy"
SELECT_RECORD: Final = "pds.select"
DELIVERY_RECORD: Final = "pds.delivery"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    v: Literal[1] = SCHEMA_VERSION
    decision_id: str
    sid: str
    evid: str
    pc: str
    probe: bool
    latency_ms: float = Field(ge=0, description="Time the PDS spent on the call.")


class PolicyRecord(_Record):
    """The ``/policy`` decision for one break."""

    record: Literal["pds.policy"] = POLICY_RECORD
    upid: str
    fv: str
    dur_ms: int
    mode: Mode
    reason: Reason
    bidders: list[str] = Field(description="Bidders the patch added.")
    devicetype: DeviceType | None
    ip_rule: IpRule = Field(description="Outcome of the viewer-IP rule; the address itself is never recorded.")
    ctx_len: int = Field(ge=0, description="Length of the catalog context header.")
    contributors: list[str] = Field(default_factory=list, description="Patch contributors whose fields are in.")
    skipped: list[str] = Field(default_factory=list, description="Patch contributors that failed or collided.")
    contributed: dict[str, JsonValue] = Field(default_factory=dict, description="Contributors' record fields.")


class BidRecord(BaseModel):
    """One bid of the auction and its outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    bidder: str
    seat: str
    impid: str
    price: Money
    dur: int | None
    vast_dur_ms: int | None
    crid: str | None
    adomain: list[str]
    cat: list[str]
    dealid: str | None
    house: bool
    tier0: bool
    outcome: Outcome


class SelectRecord(_Record):
    """The ``/select`` decision for one break."""

    record: Literal["pds.select"] = SELECT_RECORD
    dur_ms: int
    mode: Mode | None
    limit_ms: int = Field(description="The break's VAST-duration budget.")
    poddur: int
    reason: PodReason
    pod: list[str] = Field(description="Bid refs of the pod, in sequence order.")
    standalone: list[str]
    filled_ms: int = Field(description="Σ VAST durations of the pod.")
    bids: list[BidRecord] = Field(description="Every bid at or above the floor.")
    below_floor: int = Field(ge=0, description="Bids under the floor, counted only.")


def policy_record(request: PolicyRequest, decision: PolicyDecision, *, latency_ms: float) -> PolicyRecord:
    """The record of one ``/policy`` answer."""
    response = decision.response
    return PolicyRecord(
        decision_id=response.decision_id,
        sid=request.sid,
        evid=request.evid,
        pc=request.pc,
        probe=request.probe,
        latency_ms=latency_ms,
        upid=request.upid,
        fv=request.fv,
        dur_ms=request.dur_ms,
        mode=response.mode,
        reason=response.reason,
        bidders=sorted(response.bidders),
        devicetype=response.device.devicetype,
        ip_rule=decision.ip_rule,
        ctx_len=len(request.ctx),
        contributors=list(decision.contributions.applied),
        skipped=list(decision.contributions.skipped),
        contributed=decision.contributions.record,
    )


def select_record(request: SelectRequest, decision: PodDecision, *, latency_ms: float) -> SelectRecord:
    """The record of one ``/select`` answer."""
    bids = [
        BidRecord(
            ref=bid.ref,
            bidder=bid.bidder,
            seat=bid.seat,
            impid=bid.impid,
            price=bid.price,
            dur=bid.dur,
            vast_dur_ms=bid.vast_dur_ms,
            crid=bid.crid,
            adomain=bid.adomain,
            cat=bid.cat,
            dealid=bid.dealid,
            house=bid.house,
            tier0=bid.ref in decision.tier0,
            outcome=decision.outcomes[bid.ref],
        )
        for bid in request.bids
    ]
    return SelectRecord(
        decision_id=request.decision_id,
        sid=request.sid,
        evid=request.evid,
        pc=request.pc,
        probe=request.probe,
        latency_ms=latency_ms,
        dur_ms=request.dur_ms,
        mode=request.mode,
        limit_ms=decision.limit_ms,
        poddur=decision.poddur,
        reason=decision.reason,
        pod=[ad.ref for ad in decision.pod],
        standalone=[ad.ref for ad in decision.standalones],
        filled_ms=decision.filled_ms,
        bids=[bid for bid in bids if bid.ref not in decision.below_floor],
        below_floor=len(decision.below_floor),
    )


class DeliveryRecord(BaseModel):
    """One firing of our impression tracker, as the ADS log recorded it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record: Literal["pds.delivery"] = DELIVERY_RECORD
    v: Literal[1] = SCHEMA_VERSION
    decision_id: str
    sid: str
    evid: str
    crid: str
    enr: int
    pc: str = Field(description="The playback configuration that fired it.")
    fired_at: str = Field(description="When MediaTailor fired it (ISO 8601).")
    status: int | None = Field(description="The tracker's HTTP status as MediaTailor got it.")
    impression: bool = Field(description="The first firing for (sid, evid, crid): an impression.")
    fill: bool = Field(description="The first impression of the break (sid, evid): a fill.")
    credit: Credit
    line: str | None = Field(description="The own-book line the ad was reserved for.")
    amount: Money | None = Field(description="Its price (cpm / 1000), US dollars.")
