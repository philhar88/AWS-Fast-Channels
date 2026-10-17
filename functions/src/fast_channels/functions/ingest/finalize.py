"""``Finalize``: verify an encode and fix it up before Channel Assembly or MediaTailor reads it.

Every kind:

* the HLS multivariant playlist exists and has exactly the ladder's signature (Channel Assembly matches variants by
  position, channel-assembly.md §3.2);
* its BANDWIDTH and AVERAGE-BANDWIDTH become the ladder's nominal values, so slates and house ads never advertise a
  fraction of the content bitrate (Channel Assembly copies them into the channel; e2e.md §3);
* duration and, for content, the break offsets: the segment boundaries every variant shares or, when the request had
  cue points, those of them that are such boundaries.

Ads additionally get an SGAI-ready copy of their playlists under ``ads/<ladder>/<creative id>/``: MediaTailor
re-hosts only the manifests of preconditioned ads, so media playlists need absolute segment URIs on the content host
while the multivariant's child URIs stay relative (e2e.md §5.4-5.5). Segments are not copied, and the creative id
embeds the host, so a new host gets a new directory instead of rewriting manifests MediaTailor has cached
(_review.md, FinalizeAd).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr

from fast_channels.core.ingest import AssetKind
from fast_channels.core.ladder import Ladder
from fast_channels.core.manifests import (
    absolutize_media_playlist,
    expected_signature,
    ladder_signature,
    nominal_bandwidths,
    normalize_bandwidth,
    parse_multivariant,
)
from fast_channels.core.naming import creative_id, ladder_path, vod_source_name
from fast_channels.core.profiles import applicable, profile
from fast_channels.functions import aws
from fast_channels.functions.encode_timeline import duration_ms, timeline
from fast_channels.functions.ingest.storage import S3Store, Store, read_text
from fast_channels.functions.media_library import AssetStatus, asset_index, asset_key, now_iso, update_request
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

HLS: Final = profile("hls")
"""The profile of the encode's one multivariant playlist (``main.m3u8``)."""

logger = Logger()


class InvalidOutputsError(RuntimeError):
    """The encode is incomplete or does not match its ladder."""


@dataclass(frozen=True, slots=True)
class Creative:
    """The SGAI-ready copy of an ad encode."""

    creative_id: str
    ad_prefix: str
    hls_uri: str


@dataclass(frozen=True, slots=True)
class Finalized:
    """Facts about a verified encode; ``registrations`` are the VOD sources Register must create (none for ads)."""

    duration_ms: int
    break_offsets_ms: list[int]
    signature: list[list[str]]
    rewritten: list[str]
    registrations: list[dict[str, Any]] = field(default_factory=list)
    creative: Creative | None = None


@dataclass(slots=True)
class Encode:
    """The playlists of one encode, ``<prefix>main*.m3u8``."""

    ladder: Ladder
    prefix: str
    main: str
    """The multivariant playlist."""
    media: dict[str, str]
    """Variant URI (relative to ``prefix``) -> media playlist, in multivariant order."""

    @property
    def main_key(self) -> str:
        return HLS.manifest_key(self.prefix)

    @property
    def reference(self) -> str:
        """The first variant's media playlist: the encode's reference timeline."""
        return next(iter(self.media.values()))


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """``{"asset": <Prepare output>, "job_id": str}`` -> :class:`Finalized` (and the asset item ENCODED)."""
    config = settings()
    config.require("media_bucket", "library_table", "content_base_url")
    asset = event["asset"]
    logger.set_correlation_id(asset["correlation_id"])
    logger.append_keys(asset_id=asset["asset_id"], ladder_version=asset["ladder_version"], job_id=event["job_id"])
    result = finalize(
        S3Store(config.media_bucket),
        asset,
        content_base_url=config.content_base_url,
        package_profiles=config.package_profiles,
    )
    _record(asset, result, job_id=event["job_id"])
    logger.info("encode finalized", extra={"rewritten": len(result.rewritten), "duration_ms": result.duration_ms})
    return asdict(result)


def finalize(
    store: Store, asset: Mapping[str, Any], *, content_base_url: str, package_profiles: Sequence[str]
) -> Finalized:
    """Verify and fix up the encode of ``asset`` (the Prepare output) in ``store``."""
    ladder = Ladder.model_validate(asset["ladder"])
    encode = read_encode(store, ladder, asset["output_prefix"])
    rewritten = normalize(store, encode)
    kind = AssetKind(asset["kind"])
    result = Finalized(
        duration_ms=duration_ms(encode.reference),
        break_offsets_ms=break_offsets(encode, asset.get("cue_points_ms", [])) if kind is AssetKind.CONTENT else [],
        signature=[list(row) for row in ladder_signature(encode.main)],
        rewritten=rewritten,
    )
    if kind is AssetKind.AD:
        return replace(result, creative=sgai_copy(store, encode, asset["asset_id"], content_base_url))
    registration = {
        "asset_id": asset["asset_id"],
        "ladder_version": ladder.version,
        "kind": kind.value,
        "vod_source": vod_source_name(asset["asset_id"], ladder.version),
        "output_prefix": encode.prefix,
        "profiles": [p.name for p in applicable(ladder) if p.name in package_profiles],
        "correlation_id": asset["correlation_id"],
    }
    return replace(result, registrations=[registration])


def read_encode(store: Store, ladder: Ladder, prefix: str) -> Encode:
    """Read the playlists of an encode, checking that they are complete and have the ladder's signature."""
    key = HLS.manifest_key(prefix)
    main = read_text(store, key)
    if main is None:
        raise InvalidOutputsError(f"missing manifest {key}")
    if ladder_signature(main) != expected_signature(ladder):
        raise InvalidOutputsError(f"{key} does not have the variants of {ladder.version}")
    media = {v.uri: _text(store, f"{prefix}{v.uri}") for v in parse_multivariant(main)}
    return Encode(ladder=ladder, prefix=prefix, main=main, media=media)


def normalize(store: Store, encode: Encode) -> list[str]:
    """Nominal bandwidths in the multivariant playlist; returns the keys rewritten.

    ``encode`` is updated in place so the SGAI copy sees the rewritten text.
    """
    fixed = normalize_bandwidth(encode.main, nominal_bandwidths(encode.ladder, encode.main))
    if fixed == encode.main:
        return []
    encode.main = fixed
    return [_write_text(store, encode.main_key, fixed)]


def break_offsets(encode: Encode, cue_points_ms: Iterable[int]) -> list[int]:
    """Valid ad-break offsets: the segment boundaries every variant shares (``encode_timeline``), restricted to the
    cue points if any.

    Prepare snapped the cue points to the segment grid; each maps to the nearest shared boundary within half a GOP,
    and points without one are dropped with a warning.
    """
    boundaries = timeline(encode.main, encode.media.__getitem__).boundaries_ms
    cue_points = sorted(set(cue_points_ms))
    if not cue_points:
        return boundaries
    tolerance = encode.ladder.grid.gop_ms // 2
    offsets = set()
    for point in cue_points:
        nearest = min(boundaries, key=lambda b: abs(b - point), default=None)
        if nearest is not None and abs(nearest - point) <= tolerance:
            offsets.add(nearest)
        else:
            logger.warning("cue point is not a segment boundary of every variant; dropped", extra={"cue_ms": point})
    return sorted(offsets)


def sgai_copy(store: Store, encode: Encode, asset_id: str, content_base_url: str) -> Creative:
    """Write the SGAI-ready playlists of an ad under ``ads/<ladder>/<creative id>/`` (see the module docstring)."""
    base = content_base_url.rstrip("/")
    creative = creative_id(asset_id, encode.ladder.version, content_base_url)
    ad_prefix = f"ads/{ladder_path(encode.ladder.version)}/{creative}/"
    main = PurePosixPath(encode.main_key).name
    _write_text(store, f"{ad_prefix}{main}", encode.main)
    for uri, text in encode.media.items():
        _write_text(store, f"{ad_prefix}{uri}", absolutize_media_playlist(text, f"{base}/{encode.prefix}"))
    return Creative(creative_id=creative, ad_prefix=ad_prefix, hls_uri=f"{base}/{ad_prefix}{main}")


def _record(asset: Mapping[str, Any], result: Finalized, *, job_id: str) -> None:
    updated_at = now_iso()
    values: dict[str, Any] = {
        "status": AssetStatus.ENCODED.value,
        **asset_index(asset["kind"], asset["ladder_version"], AssetStatus.ENCODED, updated_at),
        "duration_ms": result.duration_ms,
        "break_offsets_ms": result.break_offsets_ms,
        "signature": result.signature,
        "job_id": job_id,
        "updated_at": updated_at,
    }
    if result.creative is not None:
        values |= asdict(result.creative)
    aws.table(settings().library_table).update_item(
        **update_request(
            asset_key(asset["asset_id"], asset["ladder_version"]),
            values,
            ConditionExpression=Attr("correlation_id").eq(asset["correlation_id"]),
        )
    )


def _text(store: Store, key: str) -> str:
    text = read_text(store, key)
    if text is None:
        raise InvalidOutputsError(f"missing {key}")
    return text


def _write_text(store: Store, key: str, text: str) -> str:
    store.write(key, text.encode())
    return key
