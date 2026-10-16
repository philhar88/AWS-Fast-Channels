"""``encoding`` tools: inspect ladder versions, propose a derived ladder, provision it.

A ladder version is immutable (``<id>@<fingerprint>``) because Channel Assembly needs every VOD source of a channel
to carry exactly the same renditions (channel-assembly.md §0.3). Changing a ladder therefore means a new version:
``propose_ladder`` applies operations to a base ladder without side effects (``core.ladder.propose``) and stores the
result as a ``PROPOSAL`` item; ``provision_ladder`` creates its MediaConvert job template and starts the ladder
provisioner, which encodes the kit (filler slate, break slates, house ads, VAST pod) on the new ladder and marks the
``LADDER`` item READY (SPEC §5.2).
"""

import json
import secrets
import time
from datetime import UTC, datetime
from typing import Final, Self

from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field

from fast_channels.core.cost import encode_cost, encode_cost_per_minute
from fast_channels.core.ladder import AudioRendition, Ladder, LadderOps, LadderOptions, VideoRendition, propose
from fast_channels.core.mediaconvert import job_template_props
from fast_channels.core.naming import ladder_key
from fast_channels.core.profiles import applicable
from fast_channels.core.timeline import BREAK_SLATE_GOPS, HOUSE_AD_GOPS
from fast_channels.functions import aws
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import _library_store as store
from fast_channels.functions.tools import dispatch
from fast_channels.functions.tools.registry import Access, Approval, ToolContext, ToolError, describe_fields, tool

logger = Logger()

describe_fields(
    LadderOps,
    codecs="Video codecs to keep (avc, hevc, av1); omit to keep all.",
    max_height="Drop video renditions taller than this many lines, e.g. 720.",
    min_height="Drop video renditions shorter than this many lines.",
    quality_shift="Added to every rendition's QVBR quality level (1-10, clamped); higher is better quality and "
    "more bits.",
    drop_rungs="Video rendition names to drop, e.g. avc-234p (encoding___get_ladder lists them).",
    new_id="Ladder id of the result (its version is <id>@<fingerprint>); omit to keep the base id.",
)

PROPOSAL_TTL_S: Final = 24 * 3600
KIT_GOPS: Final = (*BREAK_SLATE_GOPS, *HOUSE_AD_GOPS)
"""Generated kit assets whose encode a provisioning run pays for (the filler slate comes on top)."""


def _iso_s(epoch_s: int) -> str:
    return datetime.fromtimestamp(epoch_s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- list / get ------------------------------------------------------------------------------------------------------


class KitSummary(BaseModel):
    filler: str | None = Field(description="VOD source of the filler slate.")
    break_slates: list[str] = Field(description="VOD sources of the ad-break slates, shortest first.")
    house_ads: list[str] = Field(description="Creative ids of the house ads.")


class LadderSummary(BaseModel):
    ladder_version: str
    status: store.LadderStatus = Field(description="PROPOSED | PROVISIONING | READY | FAILED.")
    owner: store.Owner
    description: str | None
    codecs: list[str] | None
    max_height: int | None
    renditions: int | None = Field(description="Number of video renditions.")
    cost_per_minute_usd: float | None = Field(description="MediaConvert cost per content-minute (estimate).")
    kit: KitSummary


class ListLaddersInput(BaseModel):
    status: store.LadderStatus | None = Field(None, description="Only ladder versions in this status.")


class ListLaddersOutput(BaseModel):
    ladders: list[LadderSummary]


def definition(record: store.LadderRecord) -> Ladder | None:
    """The record's stored definition, else the built-in ladder of that version (None when neither exists)."""
    if record.ladder is not None:
        return record.ladder
    try:
        builtin = Ladder.builtin(record.ladder_id)
    except OSError, ValueError:
        return None
    return builtin if builtin.version == record.ladder_version else None


def _summary(record: store.LadderRecord, ladder: Ladder | None) -> LadderSummary:
    kit = record.kit
    return LadderSummary(
        ladder_version=record.ladder_version,
        status=record.status,
        owner=record.owner,
        description=record.description or (ladder.description if ladder else None),
        codecs=sorted(ladder.codecs) if ladder else None,
        max_height=ladder.max_height if ladder else None,
        renditions=len(ladder.video) if ladder else None,
        cost_per_minute_usd=encode_cost_per_minute(ladder) if ladder else None,
        kit=KitSummary(
            filler=kit.filler,
            break_slates=[kit.break_slates[gops] for gops in sorted(kit.break_slates)],
            house_ads=kit.house_ads,
        ),
    )


@tool("encoding", access=Access.READ)
def list_ladders(payload: ListLaddersInput, ctx: ToolContext) -> ListLaddersOutput:
    """List encoding ladder versions with status, codecs, top resolution, rendition count, encode cost per
    content-minute and kit (filler and break slates, house ads). Channels need a READY version; ingest also accepts
    a PROVISIONING one."""
    return ListLaddersOutput(ladders=[_summary(r, definition(r)) for r in store.list_ladders(payload.status)])


class LadderInput(BaseModel):
    ladder_version: str = Field(
        description="Ladder version from encoding___list_ladders, e.g. standard@86bf8088e0.", min_length=3
    )


class LadderDetail(LadderSummary):
    frame_rate: str
    gop_ms: int
    segment_ms: int
    pricing_tier: str = Field(description="MediaConvert pricing tier (BASIC or PROFESSIONAL).")
    video: list[VideoRendition] = Field(description="Video renditions in multivariant order.")
    audio: list[AudioRendition]
    options: LadderOptions
    profiles: list[str] = Field(description="Rendition profiles (manifests) every encode on this ladder writes.")
    break_slate_gops: list[int] = Field(description="Ad-break slate lengths (GOPs) the kit provides.")
    job_template: str | None
    error: str | None


@tool("encoding", access=Access.READ)
def get_ladder(payload: LadderInput, ctx: ToolContext) -> LadderDetail:
    """Get one ladder version in full: every rendition (names for propose_ladder's drop_rungs), frame rate,
    GOP/segment grid, options, pricing tier, manifest profiles, kit and provisioning status or error."""
    record = store.get_ladder(payload.ladder_version)
    ladder = definition(record) if record is not None else None
    if record is None or ladder is None:
        raise ToolError(
            "NOT_FOUND", f"unknown ladder version {payload.ladder_version}", hint="Call encoding___list_ladders."
        )
    return LadderDetail(
        **_summary(record, ladder).model_dump(),
        frame_rate=str(ladder.frame_rate),
        gop_ms=ladder.grid.gop_ms,
        segment_ms=ladder.grid.segment_ms,
        pricing_tier=ladder.pricing_tier(),
        video=list(ladder.video),
        audio=list(ladder.audio),
        options=ladder.options,
        profiles=[p.name for p in applicable(ladder)],
        break_slate_gops=sorted(record.kit.break_slates),
        job_template=record.job_template,
        error=record.error,
    )


# --- propose ---------------------------------------------------------------------------------------------------------


class ProposeLadderInput(BaseModel):
    base: str = Field(description="Ladder version to derive from, e.g. standard@86bf8088e0.", min_length=3)
    ops: LadderOps = Field(description="Changes to apply to the base; every field is optional and they combine.")


class ProposeLadderOutput(BaseModel):
    proposal_id: str = Field(description="Pass to encoding___provision_ladder (only when valid).")
    valid: bool = Field(description="False when errors is not empty; such a proposal cannot be provisioned.")
    base_version: str
    ladder_version: str | None = Field(description="Version of the proposed ladder; null when invalid.")
    existing_status: store.LadderStatus | None = Field(description="Status if this version already exists.")
    added: list[str]
    removed: list[str]
    changed: list[str] = Field(description="Renditions whose QVBR quality changed.")
    errors: list[str]
    warnings: list[str]
    video: list[str] = Field(description="Video renditions of the proposal, in order.")
    audio: list[str]
    pricing_tier: str | None
    cost_per_minute_usd: float = Field(description="Encode cost per content-minute of the proposal (estimate).")
    base_cost_per_minute_usd: float
    kit_cost_usd: float | None = Field(
        description="Estimated encode cost of the kit's break slates and house ads (filler slate extra)."
    )
    expires_at: str


class StoredProposal(BaseModel):
    """``PROPOSAL#<id>`` / ``META`` (TTL)."""

    proposal_id: str
    base_version: str
    ladder: Ladder | None
    ops: LadderOps
    errors: list[str]
    correlation_id: str
    expires_at: int

    def save(self) -> None:
        store.put_item({**store.proposal_key(self.proposal_id), **self.model_dump(mode="json")})

    @classmethod
    def load(cls, proposal_id: str) -> Self:
        item = store.get_item(store.proposal_key(proposal_id))
        if item is None or item.get("expires_at", 0) < time.time():
            raise ToolError(
                "NOT_FOUND",
                f"proposal {proposal_id} does not exist or expired",
                hint="Call encoding___propose_ladder again.",
            )
        return cls.model_validate(item)


def kit_cost(ladder: Ladder) -> float:
    """Encode cost of the generated break slates and house ads, each billed with the per-output minimum."""
    return round(sum(encode_cost(ladder, ladder.grid.gop_exact(gops)) for gops in KIT_GOPS), 2)


@tool("encoding", access=Access.READ)
def propose_ladder(payload: ProposeLadderInput, ctx: ToolContext) -> ProposeLadderOutput:
    """Propose a new ladder version derived from a base version: keep some codecs, cap or floor the height,
    shift quality, drop rungs. Changes nothing. Returns a proposal_id (valid 24 hours), the diff, validation errors
    and warnings, the encode cost per content-minute and the kit cost. Show it all to the operator; only a valid
    proposal they accept goes to encoding___provision_ladder."""
    base = store.ladder_definition(payload.base)
    proposal = propose(base, payload.ops)
    ladder = proposal.ladder
    existing = store.get_ladder(ladder.version) if ladder else None
    stored = StoredProposal(
        proposal_id=f"proposal-{secrets.token_hex(5)}",
        base_version=base.version,
        ladder=ladder,
        ops=payload.ops,
        errors=proposal.errors,
        correlation_id=ctx.correlation_id,
        expires_at=int(time.time()) + PROPOSAL_TTL_S,
    )
    stored.save()
    return ProposeLadderOutput(
        proposal_id=stored.proposal_id,
        valid=ladder is not None,
        base_version=base.version,
        ladder_version=ladder.version if ladder else None,
        existing_status=existing.status if existing else None,
        added=proposal.added,
        removed=proposal.removed,
        changed=proposal.changed,
        errors=proposal.errors,
        warnings=proposal.warnings,
        video=[r.name for r in ladder.video] if ladder else [],
        audio=[a.group_id for a in ladder.audio] if ladder else [],
        pricing_tier=ladder.pricing_tier() if ladder else None,
        cost_per_minute_usd=proposal.cost_per_minute_usd,
        base_cost_per_minute_usd=encode_cost_per_minute(base),
        kit_cost_usd=kit_cost(ladder) if ladder else None,
        expires_at=_iso_s(stored.expires_at),
    )


# --- provision -------------------------------------------------------------------------------------------------------


class ProvisionLadderInput(BaseModel):
    proposal_id: str = Field(
        description="proposal_id of a valid proposal from encoding___propose_ladder.", min_length=3
    )


class ProvisionLadderOutput(BaseModel):
    ladder_version: str
    status: store.LadderStatus
    job_template: str | None
    execution_arn: str | None = Field(description="Ladder provisioner execution; null when nothing was started.")
    message: str


def template_name(ladder_version: str) -> str:
    """``<prefix>-<id>_<fingerprint>``: the reaper deletes runtime templates named ``<prefix>*``."""
    return f"{settings().prefix}-{ladder_key(ladder_version)}"


def _create_job_template(ladder: Ladder, ctx: ToolContext) -> str:
    """CreateJobTemplate for ``ladder`` (an existing template of that name has identical settings: the name
    carries the fingerprint)."""
    config = settings()
    config.require("mediaconvert_queue_arn")
    name = template_name(ladder.version)
    request = job_template_props(ladder, name=name, queue_arn=config.mediaconvert_queue_arn, category=config.prefix)
    request["Tags"] |= {"fast:owner": store.Owner.AGENT.value, "fast:correlation-id": ctx.correlation_id}
    try:
        aws.client("mediaconvert").create_job_template(**request)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConflictException":
            raise
        logger.info("job template already exists", extra={"job_template": name})
    return name


@tool("encoding", access=Access.WRITE, approval=Approval.CONFIRM)
def provision_ladder(payload: ProvisionLadderInput, ctx: ToolContext) -> ProvisionLadderOutput:
    """Provision a proposed ladder version: create its MediaConvert job template and encode its kit (filler slate,
    break slates, house ads and ad pod) with it. Costs the kit encode (kit_cost_usd of the proposal) and takes
    minutes: poll encoding___get_ladder until READY. Only then can channels___create_channel use the version, and
    content must be ingested with it (library___ingest) to air on such a channel. Nothing is started when the
    version is already PROVISIONING or READY."""
    config = settings()
    config.require("provision_state_machine_arn")
    proposal = StoredProposal.load(payload.proposal_id)
    if proposal.ladder is None:
        raise ToolError(
            "VALIDATION",
            f"proposal {proposal.proposal_id} is invalid: {'; '.join(proposal.errors)}",
            hint="Change the operations and propose again.",
        )
    ladder = proposal.ladder
    existing = store.get_ladder(ladder.version)
    if existing is not None and existing.status in {store.LadderStatus.PROVISIONING, store.LadderStatus.READY}:
        return ProvisionLadderOutput(
            ladder_version=ladder.version,
            status=existing.status,
            job_template=existing.job_template,
            execution_arn=None,
            message=f"{ladder.version} is already {existing.status}; nothing was started.",
        )
    job_template = _create_job_template(ladder, ctx)
    store.put(
        store.LadderRecord(
            ladder_version=ladder.version,
            status=store.LadderStatus.PROVISIONING,
            ladder=ladder,
            description=ladder.description or f"derived from {proposal.base_version}",
            job_template=job_template,
            owner=store.Owner.AGENT,
            correlation_id=ctx.correlation_id,
        )
    )
    # LadderProvisioner input (infra.constructs.encoding); the provisioner keeps the kit while it re-encodes.
    request = {
        "ladder": ladder.model_dump(mode="json"),
        "job_template": job_template,
        "owner": store.Owner.AGENT.value,
        "correlation_id": ctx.correlation_id,
    }
    execution = aws.client("stepfunctions").start_execution(
        stateMachineArn=config.provision_state_machine_arn,
        name=f"kit-{ladder_key(ladder.version)}-{secrets.token_hex(4)}",
        input=json.dumps(request),
    )
    logger.info("ladder provisioning started", extra={"ladder_version": ladder.version})
    return ProvisionLadderOutput(
        ladder_version=ladder.version,
        status=store.LadderStatus.PROVISIONING,
        job_template=job_template,
        execution_arn=execution["executionArn"],
        message="Provisioning started; the ladder is READY when its kit is encoded (check encoding___get_ladder).",
    )


handler = dispatch.handler("encoding")
