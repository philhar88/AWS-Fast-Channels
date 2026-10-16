"""``origins`` target: Channel Assembly source locations and VOD sources, including content encoded elsewhere.

The deployment's own source location (``SOURCE_LOCATION``, on the media bucket) is IaC-owned, and the ingest
pipeline registers every encode on it. These tools let the agent bring pre-encoded content from an operator's S3
bucket: create a SigV4 source location for the bucket (the operator applies the returned bucket-policy statements;
the agent never edits bucket policies), register a rendition set as a VOD source once its ladder signature matches
the channel ladder, change the source groups (rendition profiles) a VOD source serves, and read alerts.
"""

import re
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Self
from urllib.parse import unquote, urljoin

from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

from fast_channels.core.ingest import ASSET_ID_PATTERN, LADDER_VERSION_PATTERN, AssetKind
from fast_channels.core.manifests import expected_signature, ladder_signature, parse_multivariant
from fast_channels.core.mediaconvert import output_prefix
from fast_channels.core.naming import agent_owned_name, is_agent_owned
from fast_channels.core.profiles import ManifestFormat, applicable, profile
from fast_channels.core.specs import http_package_configurations
from fast_channels.core.tags import resource_tags
from fast_channels.functions import aws
from fast_channels.functions.encode_timeline import Timeline, timeline
from fast_channels.functions.settings import settings
from fast_channels.functions.tools import dispatch
from fast_channels.functions.tools import library_store as store
from fast_channels.functions.tools._alerts import Alert, after_change, alerts_of
from fast_channels.functions.tools.library_store import Asset, AssetStatus, Owner
from fast_channels.functions.tools.registry import Access, Approval, ToolContext, ToolError, tool

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

logger = Logger(child=True)

MAX_PACKAGES: Final = 5  # package configurations per VOD source (channel-assembly.md §4.3 quotas)
MAX_MANIFEST_BYTES: Final = 2 * 1024 * 1024  # Channel Assembly manifest size limit (not adjustable)
MANIFEST_PATH_PATTERN: Final = r"^/\S+\.(m3u8|mpd)$"
# S3_SIGV4 needs the regional virtual-hosted form (channel-assembly.md §2.2); bucket names with dots break its TLS.
_S3_BASE_URL: Final = re.compile(
    r"^https://(?P<bucket>[a-z0-9][a-z0-9-]*[a-z0-9])\.s3\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?P<prefix>/[^?#]*)?$"
)
_ASSUMED_ROLE: Final = re.compile(r"^arn:(?P<partition>[^:]+):sts::(?P<account>\d+):assumed-role/(?P<role>[^/]+)/.+$")


def _mediatailor() -> Any:
    return aws.client("mediatailor")


# --- shared models -------------------------------------------------------------------------------------------------


class _SourceGroup(BaseModel):
    source_group: str = Field(
        description="Rendition profile served under this name (a channel output), e.g. hls, hls-avc, dash-avc."
    )

    @field_validator("source_group")
    @classmethod
    def _known_profile(cls, name: str) -> str:
        profile(name)
        return name


class Package(_SourceGroup):
    path: str = Field(
        pattern=MANIFEST_PATH_PATTERN,
        description="Manifest path relative to the source location base URL, e.g. /shows/pilot/main.m3u8.",
    )


class PackageChange(_SourceGroup):
    path: str | None = Field(
        None,
        pattern=MANIFEST_PATH_PATTERN,
        description="Manifest path; needed only for agent-registered content and a source group it does not serve "
        "yet, because pipeline encodes have every profile's manifest.",
    )


def _unique_groups[G: _SourceGroup](packages: list[G]) -> list[G]:
    groups = [p.source_group for p in packages]
    if len(set(groups)) != len(groups):
        raise ValueError("each source group may appear only once")
    return packages


# --- list_source_locations -----------------------------------------------------------------------------------------


class ListSourceLocationsInput(BaseModel):
    """No arguments."""


class SourceLocationInfo(BaseModel):
    name: str
    arn: str
    owner: Literal["iac", "agent", "external"] = Field(
        description="iac = this deployment's media bucket; agent = created by the agent; external = anything else."
    )
    access_type: str | None = Field(
        None, description="S3_SIGV4, AUTODETECT_SIGV4, SECRETS_MANAGER_ACCESS_TOKEN or none."
    )
    base_url: str
    bucket: str | None = Field(None, description="S3 bucket behind base_url, if any.")
    segment_delivery_url: str | None = Field(None, description="Where players fetch content segments.")
    created_at: str | None = None


class ListSourceLocationsOutput(BaseModel):
    source_locations: list[SourceLocationInfo]


@tool("origins", access=Access.READ)
def list_source_locations(request: ListSourceLocationsInput, ctx: ToolContext) -> ListSourceLocationsOutput:
    """List the Channel Assembly source locations of the account: this deployment's, those the agent created for
    operators' own buckets, and any others, with their bucket and segment delivery URL."""
    pages = _mediatailor().get_paginator("list_source_locations").paginate()
    return ListSourceLocationsOutput(source_locations=[_location_info(i) for page in pages for i in page["Items"]])


def _location_info(item: dict[str, Any]) -> SourceLocationInfo:
    config = settings()
    name = item["SourceLocationName"]
    base_url = item["HttpConfiguration"]["BaseUrl"]
    owner = "iac" if name == config.source_location else "agent" if is_agent_owned(name, config.prefix) else "external"
    match = _S3_BASE_URL.match(base_url)
    created = item.get("CreationTime")
    return SourceLocationInfo(
        name=name,
        arn=item["Arn"],
        owner=owner,
        access_type=item.get("AccessConfiguration", {}).get("AccessType"),
        base_url=base_url,
        bucket=match["bucket"] if match else None,
        segment_delivery_url=item.get("DefaultSegmentDeliveryConfiguration", {}).get("BaseUrl"),
        created_at=store.iso(created) if created else None,
    )


# --- create_source_location ----------------------------------------------------------------------------------------


class CreateSourceLocationInput(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$", description="Short name; created as <prefix>-agent-<name>.")
    bucket: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$",
        description="The operator's S3 bucket, in this account and region (no dots: MediaTailor reads over "
        "virtual-hosted HTTPS). SSE-S3 encryption only; SSE-KMS manifests cannot be read.",
    )
    segment_delivery_url: str = Field(
        pattern=r"^https://[^\s/?#]+(/[^\s?#]*)?$",
        description="HTTPS base URL of the CDN that serves this bucket's objects at the same paths; players fetch "
        "content segments from it.",
    )


class CreateSourceLocationOutput(BaseModel):
    name: str
    arn: str
    base_url: str
    segment_delivery_url: str
    created: bool = Field(description="False when the source location already existed for this bucket.")
    bucket_policy_statements: list[dict[str, Any]] = Field(
        description="Statements the operator must add to the bucket policy before VOD sources can be registered: "
        "MediaTailor reads scoped to this source location, and reads by the tool role (CreateVodSource checks that "
        "the caller can read the manifests)."
    )


@tool("origins", access=Access.WRITE, approval=Approval.CONFIRM)
def create_source_location(request: CreateSourceLocationInput, ctx: ToolContext) -> CreateSourceLocationOutput:
    """Create a SigV4 Channel Assembly source location for an operator's S3 bucket holding pre-encoded content.
    Returns the bucket-policy statements the operator must apply before origins___register_vod_source can read the
    bucket; show them verbatim (the agent never edits bucket policies). Idempotent for the same name and bucket."""
    config = settings()
    config.require("prefix", "region")
    if request.bucket == config.media_bucket:
        raise ToolError(
            "VALIDATION",
            f"{request.bucket} is this deployment's media bucket",
            hint=f"Its content is already served by the source location {config.source_location}.",
        )
    name = agent_owned_name(config.prefix, request.name)
    base_url = f"https://{request.bucket}.s3.{config.region}.amazonaws.com"
    existing = _describe_source_location(name)
    if existing is not None and existing["HttpConfiguration"]["BaseUrl"] != base_url:
        raise ToolError(
            "CONFLICT",
            f"source location {name} already exists for {existing['HttpConfiguration']['BaseUrl']}",
            hint="Choose another name.",
        )
    location = existing or _mediatailor().create_source_location(
        SourceLocationName=name,
        HttpConfiguration={"BaseUrl": base_url},
        AccessConfiguration={"AccessType": "S3_SIGV4"},
        DefaultSegmentDeliveryConfiguration={"BaseUrl": request.segment_delivery_url},
        Tags=_tags(ctx),
    )
    logger.info("source location ready", extra={"source_location": name, "new": existing is None})
    return CreateSourceLocationOutput(
        name=name,
        arn=location["Arn"],
        base_url=base_url,
        segment_delivery_url=location.get("DefaultSegmentDeliveryConfiguration", {}).get("BaseUrl", ""),
        created=existing is None,
        bucket_policy_statements=bucket_policy_statements(
            bucket=request.bucket, source_location_arn=location["Arn"], reader_arn=_caller_role_arn()
        ),
    )


def bucket_policy_statements(*, bucket: str, source_location_arn: str, reader_arn: str) -> list[dict[str, Any]]:
    """Bucket-policy statements that let Channel Assembly and the tool role read a bring-your-own bucket.

    MediaTailor presents the source location ARN as ``aws:SourceArn`` (verified positively and negatively,
    channel-assembly.md §2.3). CreateVodSource also checks that the *caller* can read the manifests (§2.1), hence
    the second statement for the tool role.
    """
    partition, _, _, account = source_location_arn.split(":")[1:5]
    objects = f"arn:{partition}:s3:::{bucket}/*"
    sid = "".join(part.capitalize() for part in source_location_arn.rsplit("/", 1)[-1].split("-"))
    return [
        {
            "Sid": f"MediaTailorRead{sid}",
            "Effect": "Allow",
            "Principal": {"Service": "mediatailor.amazonaws.com"},
            "Action": "s3:GetObject",
            "Resource": objects,
            "Condition": {
                "StringEquals": {"aws:SourceAccount": account},
                "ArnLike": {"aws:SourceArn": source_location_arn},
            },
        },
        {
            "Sid": f"FastChannelsToolsRead{sid}",
            "Effect": "Allow",
            "Principal": {"AWS": reader_arn},
            "Action": "s3:GetObject",
            "Resource": objects,
        },
    ]


def _caller_role_arn() -> str:
    """IAM role ARN of this function (STS reports the assumed-role session; Lambda roles have no path)."""
    arn: str = aws.client("sts").get_caller_identity()["Arn"]
    if match := _ASSUMED_ROLE.match(arn):
        return f"arn:{match['partition']}:iam::{match['account']}:role/{match['role']}"
    return arn


def _describe_source_location(name: str) -> dict[str, Any] | None:
    try:
        location: dict[str, Any] = _mediatailor().describe_source_location(SourceLocationName=name)
    except ClientError as error:
        if error.response["Error"]["Code"] == "NotFoundException":
            return None
        raise
    return location


# --- register_vod_source -------------------------------------------------------------------------------------------


class RegisterVodSourceInput(BaseModel):
    asset_id: str = Field(pattern=ASSET_ID_PATTERN, description="Library id to give the content, e.g. acme-pilot.")
    source_location: str = Field(description="Source location holding the files (origins___list_source_locations).")
    packages: Annotated[list[Package], AfterValidator(_unique_groups)] = Field(
        min_length=1,
        max_length=MAX_PACKAGES,
        description="One manifest per source group, at least one HLS. A channel airs the asset only if every "
        "channel output has a package here.",
    )
    ladder_version: str | None = Field(
        None, pattern=LADDER_VERSION_PATTERN, description="Ladder the content must match; omit for the default."
    )
    content_id: str | None = Field(None, min_length=1, max_length=64, description="Catalog id of the title.")
    title: str | None = Field(None, max_length=200, description="Display title recorded on the asset.")


class RegisterVodSourceOutput(BaseModel):
    asset_id: str
    ladder_version: str
    vod_source: str
    vod_source_arn: str
    source_location: str
    duration_ms: int
    break_offset_count: int = Field(description="Valid ad-break offsets found (library___get_asset lists them).")
    source_groups: list[str]
    missing_source_groups: list[str] = Field(
        description="Package profiles of this deployment the asset lacks; channels with those outputs cannot air it."
    )
    alerts: list[Alert] = Field(description="Current alerts of the new VOD source (noise filtered).")


@tool("origins", access=Access.WRITE)
def register_vod_source(request: RegisterVodSourceInput, ctx: ToolContext) -> RegisterVodSourceOutput:
    """Register pre-encoded HLS/DASH content from a source location as a schedulable library asset (owner agent).
    Every HLS manifest must match the ladder exactly (codec, resolution, frame rate and audio group of every
    variant, in order; LADDER_MISMATCH otherwise) because Channel Assembly switches programs variant by variant.
    Duration and ad-break offsets are computed from the media playlists; the asset is READY at once."""
    config = settings()
    config.require("prefix", "library_table")
    ladder_version = request.ladder_version or _default_ladder_version()
    ladder = store.ladder_definition(ladder_version)
    _check_deployment_groups(request.packages)
    if store.get_asset(request.asset_id, ladder_version) is not None:
        raise ToolError(
            "CONFLICT",
            f"{request.asset_id} already exists with {ladder_version}",
            hint="Change its packages with origins___update_package_configurations, or pick another asset_id.",
        )
    location = _require_source_location(request.source_location)
    manifests = _S3Manifests.of(location)
    texts = {p.source_group: manifests.verified(p.source_group, p.path, ladder) for p in request.packages}
    reference = _timeline_package(request.packages)
    encode = manifests.timeline(reference.path, texts[reference.source_group])

    name = agent_owned_name(config.prefix, request.asset_id, ladder_version.partition("@")[2])
    arn = _create_vod_source(request.source_location, name, request.packages, _tags(ctx, ladder_version))
    store.put(
        Asset(
            asset_id=request.asset_id,
            ladder_version=ladder_version,
            kind=AssetKind.CONTENT,
            status=AssetStatus.READY,
            owner=Owner.AGENT,
            title=request.title,
            content_id=request.content_id,
            duration_ms=encode.duration_ms,
            break_offsets_ms=encode.boundaries_ms,
            signature=[list(row) for row in ladder_signature(texts[reference.source_group])],
            vod_source=name,
            source_location=request.source_location,
            packages={p.source_group: p.path for p in request.packages},
            correlation_id=ctx.correlation_id,
        ),
        condition=Attr("pk").not_exists(),
    )
    logger.info("vod source registered", extra={"asset_id": request.asset_id, "vod_source": name})
    groups = [p.source_group for p in request.packages]
    return RegisterVodSourceOutput(
        asset_id=request.asset_id,
        ladder_version=ladder_version,
        vod_source=name,
        vod_source_arn=arn,
        source_location=request.source_location,
        duration_ms=encode.duration_ms,
        break_offset_count=len(encode.boundaries_ms),
        source_groups=groups,
        missing_source_groups=[g for g in settings().package_profiles if g not in groups],
        alerts=after_change(lambda: arn),
    )


def _default_ladder_version() -> str:
    if version := settings().default_ladder_version:
        return version
    raise ToolError("VALIDATION", "no default ladder is configured", hint="Pass ladder_version.")


def _check_deployment_groups(packages: Iterable[_SourceGroup]) -> None:
    allowed = settings().package_profiles
    if unknown := [p.source_group for p in packages if allowed and p.source_group not in allowed]:
        raise ToolError(
            "VALIDATION",
            f"source groups {unknown} are not package profiles of this deployment",
            hint=f"Use some of: {', '.join(allowed)}.",
        )


def _timeline_package(packages: Sequence[Package]) -> Package:
    """The HLS package whose media playlists define duration and break offsets (``hls`` when present)."""
    hls = [p for p in packages if profile(p.source_group).format is ManifestFormat.HLS]
    if not hls:
        raise ToolError("VALIDATION", "at least one HLS package is required", hint="Add the hls source group.")
    return next((p for p in hls if p.source_group == "hls"), hls[0])


def _require_source_location(name: str) -> dict[str, Any]:
    location = _describe_source_location(name)
    if location is None:
        raise ToolError(
            "NOT_FOUND", f"no source location {name}", hint="Call origins___list_source_locations for the names."
        )
    return location


def _create_vod_source(location: str, name: str, packages: Sequence[Package], tags: dict[str, str]) -> str:
    """Create the VOD source, or adopt an identical one left by an earlier attempt; return its ARN."""
    configurations = _configurations((p.source_group, p.path) for p in packages)
    try:
        created = _mediatailor().create_vod_source(
            SourceLocationName=location, VodSourceName=name, HttpPackageConfigurations=configurations, Tags=tags
        )
    except ClientError as error:
        message = error.response["Error"].get("Message", "")
        if "already exists" in message:
            existing = _mediatailor().describe_vod_source(SourceLocationName=location, VodSourceName=name)
            if _same_configurations(existing["HttpPackageConfigurations"], configurations):
                return str(existing["Arn"])
            raise ToolError("CONFLICT", f"VOD source {name} already exists with other packages") from error
        if error.response["Error"]["Code"] == "BadRequestException" and "permission" in message.lower():
            raise ToolError(
                "FORBIDDEN",
                message,
                retryable=True,
                hint="Apply the bucket-policy statements from origins___create_source_location (MediaTailor and the "
                "tool role must read the manifests), wait a minute for propagation, then retry.",
            ) from error
        raise
    return str(created["Arn"])


def _configurations(packages: Iterable[tuple[str, str]]) -> list[dict[str, str]]:
    """HttpPackageConfigurations; the Type follows the profile, since CreateVodSource accepts any Type."""
    return [{"Path": path, "SourceGroup": group, "Type": profile(group).format.value} for group, path in packages]


def _same_configurations(left: Iterable[dict[str, str]], right: Iterable[dict[str, str]]) -> bool:
    def normalised(configurations: Iterable[dict[str, str]]) -> set[tuple[str, str, str]]:
        return {(c["SourceGroup"], c["Path"], c["Type"]) for c in configurations}

    return normalised(left) == normalised(right)


class _S3Manifests:
    """Reads manifests of an S3-backed source location with the tool role's own credentials."""

    def __init__(self, bucket: str, prefix: str) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")

    @classmethod
    def of(cls, location: dict[str, Any]) -> Self:
        base_url = location["HttpConfiguration"]["BaseUrl"]
        if not (match := _S3_BASE_URL.match(base_url)):
            raise ToolError(
                "VALIDATION",
                f"{location['SourceLocationName']} is not on S3 ({base_url})",
                hint="Register content from S3 source locations (origins___create_source_location).",
            )
        return cls(match["bucket"], match["prefix"] or "")

    def read(self, path: str) -> str:
        key = "/".join(filter(None, (self.prefix, unquote(path).lstrip("/"))))
        try:
            response = aws.client("s3").get_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code in {"NoSuchKey", "404"}:
                raise ToolError("NOT_FOUND", f"no manifest at s3://{self.bucket}/{key}") from error
            if code in {"AccessDenied", "403"}:
                raise ToolError(
                    "FORBIDDEN",
                    f"cannot read s3://{self.bucket}/{key}",
                    hint="The operator must apply the bucket-policy statements from origins___create_source_location.",
                ) from error
            raise
        if response.get("ContentLength", 0) > MAX_MANIFEST_BYTES:
            raise ToolError("VALIDATION", f"{path} exceeds the 2 MB Channel Assembly manifest limit")
        return str(response["Body"].read().decode("utf-8"))

    def verified(self, source_group: str, path: str, ladder: Ladder) -> str:
        """The manifest at ``path``, after checking its format and, for HLS, its ladder signature for the group."""
        text = self.read(path)
        if profile(source_group).format is ManifestFormat.DASH:
            if "<MPD" not in text:
                raise ToolError("VALIDATION", f"{path} is not a DASH MPD")
            return text
        if not text.lstrip().startswith("#EXTM3U") or not parse_multivariant(text):
            raise ToolError("VALIDATION", f"{path} is not an HLS multivariant playlist")
        expected = expected_signature(ladder, profile(source_group))
        if (actual := ladder_signature(text)) != expected:
            raise ToolError(
                "LADDER_MISMATCH",
                f"{path} does not match {ladder.version} for {source_group}: {_difference(expected, actual)}",
                hint="Channel Assembly needs identical renditions in identical order; re-encode with the ladder or "
                "choose a ladder that matches the content.",
            )
        return text

    def timeline(self, path: str, multivariant: str) -> Timeline:
        """Duration and break offsets (``encode_timeline``) of the multivariant playlist at ``path``, whose media
        playlists must be complete VOD playlists."""
        return timeline(multivariant, lambda uri: self._vod_playlist(_child(path, uri), uri))

    def _vod_playlist(self, path: str, uri: str) -> str:
        text = self.read(path)
        if "#EXT-X-ENDLIST" not in text or "#EXTINF:" not in text:
            raise ToolError("VALIDATION", f"{uri} is not a complete VOD media playlist (#EXT-X-ENDLIST)")
        return text


def _child(manifest_path: str, uri: str) -> str:
    if "://" in uri:
        raise ToolError("VALIDATION", f"{uri}: child playlist URIs must be relative to the multivariant")
    return urljoin(manifest_path, uri)


def _difference(expected: Sequence[Sequence[str]], actual: Sequence[Sequence[str]]) -> str:
    rows = zip(expected, actual, strict=False)
    if diff := next(((n, want, got) for n, (want, got) in enumerate(rows, 1) if want != got), None):
        n, want, got = diff
        return f"variant {n} is {'/'.join(got)} but the ladder has {'/'.join(want)}"
    return f"{len(actual)} variants but the ladder has {len(expected)}"


# --- update_package_configurations ---------------------------------------------------------------------------------


class UpdatePackagesInput(BaseModel):
    asset_id: str = Field(pattern=ASSET_ID_PATTERN, description="Library asset whose VOD source changes.")
    ladder_version: str | None = Field(
        None, pattern=LADDER_VERSION_PATTERN, description="Which encode; omit for the deployment default."
    )
    packages: Annotated[list[PackageChange], AfterValidator(_unique_groups)] = Field(
        min_length=1,
        max_length=MAX_PACKAGES,
        description="The complete new set of source groups; it replaces the current one.",
    )


class UpdatePackagesOutput(BaseModel):
    asset_id: str
    ladder_version: str
    vod_source: str
    source_groups: list[str]
    added: list[str]
    removed: list[str] = Field(description="Channels with these outputs can no longer air this asset.")
    alerts: list[Alert] = Field(description="Current alerts of the VOD source (noise filtered).")


@tool("origins", access=Access.WRITE, approval=Approval.CONFIRM)
def update_package_configurations(request: UpdatePackagesInput, ctx: ToolContext) -> UpdatePackagesOutput:
    """Replace the source groups (rendition profiles) an asset's VOD source serves. Channel Assembly needs every
    program, slate and filler of a channel to serve every output of the channel, so add a group before a channel
    with that output airs the asset. Pass the complete new set."""
    config = settings()
    config.require("library_table", "source_location")
    ladder_version = request.ladder_version or _default_ladder_version()
    asset = store.get_asset(request.asset_id, ladder_version)
    if asset is None or asset.vod_source is None:
        raise ToolError(
            "NOT_FOUND",
            f"{request.asset_id} has no VOD source with {ladder_version}",
            hint="Ads have no VOD source; check the asset with library___get_asset.",
        )
    _check_deployment_groups(request.packages)
    ladder = store.ladder_definition(ladder_version)
    paths = {change.source_group: _package_path(asset, ladder, change) for change in request.packages}
    location = asset.source_location or config.source_location
    if asset.owner is Owner.AGENT and (new := {g: p for g, p in paths.items() if p != asset.packages.get(g)}):
        manifests = _S3Manifests.of(_require_source_location(location))
        for group, path in new.items():
            manifests.verified(group, path, ladder)

    client = _mediatailor()
    current = client.describe_vod_source(SourceLocationName=location, VodSourceName=asset.vod_source)
    before = {c["SourceGroup"] for c in current["HttpPackageConfigurations"]}
    updated = client.update_vod_source(
        SourceLocationName=location,
        VodSourceName=asset.vod_source,
        HttpPackageConfigurations=_configurations(paths.items()),
    )
    store.put(asset.model_copy(update={"packages": paths}))
    logger.info("vod source packages updated", extra={"vod_source": asset.vod_source, "source_groups": list(paths)})
    return UpdatePackagesOutput(
        asset_id=asset.asset_id,
        ladder_version=ladder_version,
        vod_source=asset.vod_source,
        source_groups=list(paths),
        added=sorted(paths.keys() - before),
        removed=sorted(before - paths.keys()),
        alerts=after_change(lambda: str(updated["Arn"])),
    )


def _package_path(asset: Asset, ladder: Ladder, change: PackageChange) -> str:
    """Path of a source group's manifest: given, already registered, or the pipeline's AdditionalManifest."""
    if change.path or (change.source_group in asset.packages):
        return change.path or asset.packages[change.source_group]
    target = profile(change.source_group)
    if asset.owner is not Owner.AGENT and target in applicable(ladder):
        (configuration,) = http_package_configurations(output_prefix(asset.asset_id, ladder.version), [target])
        return configuration["Path"]
    raise ToolError(
        "VALIDATION",
        f"no {change.source_group} manifest is known for {asset.asset_id}",
        hint="Pass its path (external content), or pick a profile the ladder has renditions for.",
    )


# --- list_alerts ---------------------------------------------------------------------------------------------------


class ListAlertsInput(BaseModel):
    channel: str | None = Field(None, description="Channel name: alerts of the channel and its programs.")
    vod_source: str | None = Field(None, description="VOD source name (library___get_asset shows it).")
    source_location: str | None = Field(
        None,
        description="Source location of vod_source (default: this deployment's); alone, the source location itself.",
    )
    include_noise: bool = Field(
        False, description="Also return SEGMENT_DURATION_MISMATCH and BANDWIDTH_MISMATCH, which are expected here."
    )
    limit: int = Field(50, ge=1, le=100, description="Maximum number of alerts to return, most severe first.")

    @model_validator(mode="after")
    def _one_resource(self) -> Self:
        targets = (self.channel is not None, self.vod_source is not None or self.source_location is not None)
        if sum(targets) != 1:
            raise ValueError("pass channel, or vod_source (with its source_location), or source_location")
        return self


class ListAlertsOutput(BaseModel):
    resource_arn: str
    alerts: list[Alert] = Field(description="SCHEDULING_ERROR first, then PLAYBACK_WARNING and INFO; newest first.")
    scheduling_errors: int = Field(description="SCHEDULING_ERROR alerts: these break playback and must be fixed.")
    suppressed: int = Field(description="Expected SEGMENT_DURATION_MISMATCH/BANDWIDTH_MISMATCH alerts left out.")
    truncated: bool = Field(description="True when more alerts exist than limit.")


@tool("origins", access=Access.READ)
def list_alerts(request: ListAlertsInput, ctx: ToolContext) -> ListAlertsOutput:
    """List Channel Assembly alerts of a channel (and its programs), a VOD source or a source location, most severe
    first. Check a channel after every schedule change and before starting it: SCHEDULING_ERROR alerts break
    playback. The expected SEGMENT_DURATION_MISMATCH and BANDWIDTH_MISMATCH warnings are left out (counted in
    suppressed) unless include_noise is set."""
    arn = _resource_arn(request)
    alerts, suppressed = alerts_of(arn, include_noise=request.include_noise)
    return ListAlertsOutput(
        resource_arn=arn,
        alerts=alerts[: request.limit],
        scheduling_errors=sum(a.category == "SCHEDULING_ERROR" for a in alerts),
        suppressed=suppressed,
        truncated=len(alerts) > request.limit,
    )


def _resource_arn(request: ListAlertsInput) -> str:
    client = _mediatailor()
    if request.channel is not None:
        return str(client.describe_channel(ChannelName=request.channel)["Arn"])
    location = request.source_location or settings().source_location
    if request.vod_source is not None:
        return str(client.describe_vod_source(SourceLocationName=location, VodSourceName=request.vod_source)["Arn"])
    return str(_require_source_location(location)["Arn"])


def _tags(ctx: ToolContext, ladder_version: str | None = None) -> dict[str, str]:
    """CONTRACTS §E tags of an agent-created resource."""
    return resource_tags(settings().app, Owner.AGENT, ladder_version=ladder_version, correlation_id=ctx.correlation_id)


handler = dispatch.handler("origins")
