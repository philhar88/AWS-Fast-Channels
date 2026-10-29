"""Wire models of the PDS contract, version 1 (contracts/pds-policy.schema.json, contracts/pds-select.schema.json).

Both directions are strict (unknown fields are refused) and carry ``v``: a change to either side is a new version, so
a module and a PDS that disagree fail visibly, and safely. A refused or failed ``/policy`` call leaves the stored
request alone (own book and house only, no third party); a failed ``/select`` call makes the module render its house
fallback.
"""

from decimal import Decimal
from enum import IntEnum, StrEnum
from ipaddress import IPv4Address, IPv6Address
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, PlainSerializer, WithJsonSchema, model_validator

SCHEMA_VERSION: Final = 1
"""Contract version: ``v`` in every request and response."""

TICKET_PATTERN: Final = r"^[A-Za-z0-9_-]{0,22}$"
"""``t`` as the module forwards it: ``X-Fast-T`` with every character outside ``[A-Za-z0-9_-]`` removed, cut to 22."""
SID_PATTERN: Final = r"^[A-Za-z0-9-]{1,64}$"
"""MediaTailor session id (``X-Fast-Sid: {{session.uuid}}``)."""
PC_PATTERN: Final = r"^[A-Za-z0-9_-]{1,64}$"
"""Playback configuration name (``X-Fast-Pc``)."""
EVID_PATTERN: Final = r"^[0-9]{1,10}$"
"""SCTE-35 event id of the break (``[scte.event_id]``, 31 bits). Required: it makes ``decision_id`` unique per break,
so a request without one is refused (the module then serves the own book and house ads)."""
OWN_BOOK: Final = "ownbook"
"""Prebid Server bidder alias of the own book."""
NAME_PATTERN: Final = r"^[A-Za-z0-9_.-]{1,64}$"
"""Bidder, imp and bid-reference names."""

type Money = Annotated[
    Decimal,
    Field(ge=0),
    PlainSerializer(float, return_type=float, when_used="json"),
    WithJsonSchema({"type": "number", "minimum": 0}),
]
"""US dollars; prices are CPM (per 1,000 impressions), as in OpenRTB. JSON carries them as numbers."""


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Mode(StrEnum):
    """Privacy mode of one break: what third parties may receive (docs/pds.md#privacy-modes)."""

    FULL = "FULL"
    """Consented adult with separate video-privacy consent: per-partner pseudonymous ids and scoped user data."""
    PSEUDONYMOUS = "PSEUDONYMOUS"
    """Authenticated, no sale/share opt-out: no ids; genre and category, never the title."""
    CONTEXTUAL = "CONTEXTUAL"
    """Guests, ticketless sessions, invalid or replayed tickets, opted-out viewers: no ids, no user data."""
    SAFE = "SAFE"
    """Consent unknown, an authenticated viewer's identity failed, or a probe: own book and house only."""
    KIDS = "KIDS"
    """Child-directed: COPPA set, house and approved direct lines only."""


class Reason(StrEnum):
    """Why a break got its mode."""

    BOUND = "bound"
    """The ticket is bound to this session and configuration."""
    MISSING = "missing"
    """No ticket: the player played the plain URL, or the edge removed a malformed one."""
    INVALID = "invalid"
    """A ticket that is not 22 URL-safe characters."""
    UNKNOWN = "unknown"
    """A well-formed ticket with no binding row."""
    REPLAY = "replay"
    """The ticket is bound to another session."""
    PC_MISMATCH = "pc_mismatch"
    """The ticket was minted for another playback configuration."""
    EXPIRED_OR_RACED = "expired_or_raced"
    """The binding lease ended (idle 2 h, absolute 24 h) or the redeem window passed unredeemed."""
    OPTED_OUT = "opted_out"
    """A bound viewer who opted out of sale/share."""
    CONSENT_UNAVAILABLE = "consent_unavailable"
    """The consent store failed or its answer is older than 60 s."""
    PROBE = "probe"
    """A synthetic probe session (``X-Fast-Probe``)."""
    KIDS = "kids"
    """A kids playback configuration or a child-directed channel."""


class DeviceType(IntEnum):
    """AdCOM 1.0 device types (OpenRTB ``device.devicetype``)."""

    MOBILE_TABLET = 1
    PERSONAL_COMPUTER = 2
    CONNECTED_TV = 3
    PHONE = 4
    TABLET = 5
    CONNECTED_DEVICE = 6
    SET_TOP_BOX = 7
    OOH_DEVICE = 8


class AdmKind(StrEnum):
    """What a bid's ``adm`` holds, as the module parsed it."""

    INLINE = "InLine"
    WRAPPER = "Wrapper"


def decision_id(sid: str, evid: str) -> str:
    """Id of one session's decision on one break, ``<sid>.<evid>``: the join key of records, trackers and ledger."""
    return f"{sid}.{evid}"


# --- /policy -------------------------------------------------------------------------------------------------------


class PolicyRequest(_Wire):
    """``POST /policy``: what the module knows about the break before the auction."""

    v: Literal[1]
    t: str = Field(default="", pattern=TICKET_PATTERN, description="Playback ticket, empty when the session has none.")
    sid: str = Field(pattern=SID_PATTERN, description="MediaTailor session id.")
    pc: str = Field(pattern=PC_PATTERN, description="Calling playback configuration.")
    fv: str = Field(
        default="", pattern=r"^[A-Za-z0-9_-]{0,32}$", description="Release of the configuration's Functions."
    )
    evid: str = Field(pattern=EVID_PATTERN, description="SCTE-35 event id of the break.")
    upid: str = Field(default="", max_length=255, description="Segmentation UPID of the break: the content id.")
    dur_ms: int = Field(ge=0, description="Break duration as MediaTailor requested it (session.avail_duration_ms).")
    ua: str = Field(default="", max_length=2048, description="User-Agent of the ADS request: the viewer's.")
    xff: str = Field(default="", max_length=8192, description="X-Forwarded-For as Prebid Server received it.")
    cfva: str = Field(default="", max_length=64, description="CloudFront-Viewer-Address at Prebid Server, if any.")
    ctx: str = Field(default="", max_length=1000, description="Catalog context header (x-fast-ctx), if any.")
    probe: bool = Field(default=False, description="X-Fast-Probe: a synthetic probe session.")

    @property
    def decision_id(self) -> str:
        """``<sid>.<evid>``."""
        return decision_id(self.sid, self.evid)


class Regs(_Wire):
    """OpenRTB ``regs`` the module sets."""

    coppa: Literal[0, 1] = 0
    gpp: str | None = Field(default=None, max_length=4096, description="GPP string, where known.")
    gpp_sid: list[int] | None = Field(default=None, description="GPP section ids applicable, where known.")


class Device(_Wire):
    """OpenRTB ``device`` fields the module sets. A null ``ip``/``ipv6`` makes the module remove that field."""

    ip: IPv4Address | None = Field(default=None, description="Viewer IPv4 address from the viewer-IP rule.")
    ipv6: IPv6Address | None = Field(default=None, description="Viewer IPv6 address from the viewer-IP rule.")
    devicetype: DeviceType | None = Field(default=None, description="AdCOM device type from the User-Agent.")


class Fpd(_Wire):
    """First-party data scoping: the bidders allowed to receive first-party data (``ext.prebid.data.bidders``)."""

    bidders: list[str] = Field(default_factory=list)


class Deal(_Wire):
    """An OpenRTB ``imp.pmp.deals`` entry the module adds to every imp."""

    id: str = Field(min_length=1, max_length=128)
    bidfloor: Money | None = None
    at: Literal[1, 2, 3] | None = None
    wseat: list[str] = Field(default_factory=list)


class PolicyResponse(_Wire):
    """``/policy`` answer: the patch the module applies verbatim to the bid request; it never removes the own book."""

    v: Literal[1] = SCHEMA_VERSION
    decision_id: str
    mode: Mode
    reason: Reason
    bidders: dict[str, dict[str, JsonValue]] = Field(
        default_factory=dict, description="Bidders added to every imp, with their parameters (none in SAFE and KIDS)."
    )
    regs: Regs = Regs()
    device: Device = Device()
    app_content: dict[str, JsonValue] | None = Field(
        default=None, description="OpenRTB app.content fields (show-level metadata), or null for none."
    )
    user: dict[str, JsonValue] | None = Field(default=None, description="OpenRTB user object; FULL only, else null.")
    fpd: Fpd = Fpd()
    deals: list[Deal] = Field(default_factory=list, description="Deals for every imp; FULL only.")


# --- /select -------------------------------------------------------------------------------------------------------


class Slot(_Wire):
    """One imp of the pod, in pod order."""

    impid: str = Field(pattern=NAME_PATTERN)
    poddur: int = Field(gt=0, description="The pod's total duration in seconds (video.poddur).")
    rqddurs: list[Annotated[int, Field(gt=0)]] = Field(
        default_factory=list, description="Exact ad durations the slot accepts, in seconds (video.rqddurs)."
    )


class Bid(_Wire):
    """One bid of the auction, with what the module read from its ``adm``."""

    ref: str = Field(pattern=NAME_PATTERN, description="The module's id of this bid, unique in the request.")
    bidder: str = Field(pattern=NAME_PATTERN)
    seat: str = Field(default="", max_length=64)
    impid: str = Field(pattern=NAME_PATTERN)
    price: Money = Field(description="CPM, US dollars.")
    dur: int | None = Field(default=None, ge=0, description="Declared ad duration, seconds (bid.dur).")
    vast_dur_ms: int | None = Field(default=None, ge=0, description="Linear <Duration> of the adm, milliseconds.")
    crid: str | None = Field(default=None, max_length=256)
    adomain: list[str] = Field(default_factory=list)
    cat: list[str] = Field(default_factory=list)
    cattax: int | None = Field(default=None, ge=1, description="Taxonomy of cat; OpenRTB's default is 1.")
    dealid: str | None = Field(default=None, max_length=128)
    house: bool = Field(
        default=False, description="An own-book house ad (ext.fast.house): a filler, never sold. Ignored for others."
    )
    has_impression: bool = Field(default=False, description="The adm already has an <Impression>.")
    adm_kind: AdmKind | None = Field(default=None, description="InLine or Wrapper; null when adm is not usable VAST.")


class SelectRequest(_Wire):
    """``POST /select``: every bid of the auction, after it ran."""

    v: Literal[1]
    decision_id: str
    sid: str = Field(pattern=SID_PATTERN)
    pc: str = Field(pattern=PC_PATTERN)
    evid: str = Field(pattern=EVID_PATTERN)
    dur_ms: int = Field(ge=0)
    mode: Mode | None = Field(description="Mode of the applied /policy patch; null when the module applied none.")
    probe: bool = False
    slots: list[Slot] = Field(min_length=1, max_length=16)
    bids: list[Bid] = Field(default_factory=list, max_length=512)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.decision_id != decision_id(self.sid, self.evid):
            raise ValueError(f"decision_id must be {decision_id(self.sid, self.evid)!r}")
        impids = [slot.impid for slot in self.slots]
        if len(set(impids)) != len(impids):
            raise ValueError("slot impids must be unique")
        refs = [bid.ref for bid in self.bids]
        if len(set(refs)) != len(refs):
            raise ValueError("bid refs must be unique")
        if strays := sorted({bid.impid for bid in self.bids} - set(impids)):
            raise ValueError(f"bids for unknown imps: {strays}")
        return self


class PodAd(_Wire):
    """A sequenced ad of the pod."""

    impid: str
    ref: str
    sequence: int = Field(ge=1)


class StandaloneAd(_Wire):
    """A standalone ad (no ``sequence``): the replacement MediaTailor uses if this slot's sequenced ad fails."""

    impid: str
    ref: str


class PodReason(StrEnum):
    """Outcome of pod selection."""

    FILLED = "filled"
    """Every slot has an ad."""
    PARTIAL = "partial"
    """Some slots have none; Channel Assembly's break slate plays the rest."""
    EMPTY = "empty"
    """No eligible ad."""
    SHORT_AVAIL = "short_avail"
    """The avail is shorter than one 30 s slot: an empty pod by rule."""


class SelectResponse(_Wire):
    """``/select`` answer: exactly the pod the module renders, and our trackers for every ad in it."""

    v: Literal[1] = SCHEMA_VERSION
    pod: list[PodAd] = Field(default_factory=list)
    standalone: list[StandaloneAd] = Field(default_factory=list)
    trackers: dict[str, list[str]] = Field(
        default_factory=dict, description="Impression URLs per bid ref, for every ad in pod and standalone."
    )
    reason: PodReason
