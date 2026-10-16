"""``Finalize``: verify an encode and fix it up before Channel Assembly or MediaTailor reads it.

Every kind:

* every applicable profile manifest exists, and each HLS one has exactly the ladder's signature (Channel Assembly
  matches variants by position, channel-assembly.md §3.2);
* AV1 init segments declare the rung's real level and bit depth (MediaConvert writes level 2.0, 8-bit;
  ladders.md §3.3 item 1);
* multivariant BANDWIDTH and AVERAGE-BANDWIDTH become the ladder's nominal values, so slates and house ads never
  advertise a fraction of the content bitrate (Channel Assembly copies them into the channel; e2e.md §3);
* I-frame playlists are raised to EXT-X-VERSION 5 when the ladder writes them;
* duration and, for content, the break offsets: the boundaries every track shares or, when the request had cue
  points, those of them that are such boundaries.

Ads additionally get an SGAI-ready copy of their manifests under ``ads/<ladder>/<creative id>/``: MediaTailor
re-hosts only the manifests of preconditioned ads, so media playlists need absolute segment and EXT-X-MAP URIs on
the content host while the multivariant's child URIs stay relative (e2e.md §5.4-5.5). The DASH manifest gets
absolute segment templates: MediaTailor resolves them against the copy's location and ignores ``BaseURL``. Segments
are not copied, and the creative id embeds the host, so a new host gets a new directory instead of rewriting
manifests MediaTailor has cached (_review.md, FinalizeAd).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr

from fast_channels.core.captions import CaptionPlan
from fast_channels.core.ingest import AssetKind
from fast_channels.core.ladder import Ladder, VideoCodec, VideoRendition
from fast_channels.core.manifests import (
    absolutize_media_playlist,
    absolutize_mpd,
    attributes,
    bump_iframe_version,
    expected_signature,
    ladder_signature,
    nominal_bandwidths,
    normalize_bandwidth,
    parse_multivariant,
)
from fast_channels.core.mp4 import av1_level_idx, patch_av1c
from fast_channels.core.naming import creative_id, ladder_path, vod_source_name
from fast_channels.core.profiles import ManifestFormat, RenditionProfile, applicable, profile
from fast_channels.functions import aws
from fast_channels.functions.encode_timeline import duration_ms, timeline
from fast_channels.functions.ingest.storage import S3Store, Store, read_text
from fast_channels.functions.media_library import AssetStatus, asset_index, asset_key, now_iso, update_request
from fast_channels.functions.settings import settings

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

_CHILD_TAGS: Final = ("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:")
_MAP_URI: Final = re.compile(r'^#EXT-X-MAP:.*?URI="([^"]+)"', re.MULTILINE)

logger = Logger()


class InvalidOutputsError(RuntimeError):
    """The encode is incomplete or does not match its ladder."""


@dataclass(frozen=True, slots=True)
class Creative:
    """The SGAI-ready copy of an ad encode."""

    creative_id: str
    ad_prefix: str
    hls_uri: str
    dash_uri: str


@dataclass(frozen=True, slots=True)
class Finalized:
    """Facts about a verified encode; ``registrations`` are the VOD sources Register must create (none for ads)."""

    duration_ms: int
    break_offsets_ms: list[int]
    signature: list[list[str]]
    rewritten: list[str]
    registrations: list[dict[str, Any]] = field(default_factory=list)
    creative: Creative | None = None


@dataclass(frozen=True, slots=True)
class Encode:
    """The manifests of one encode, ``<prefix>main*``."""

    ladder: Ladder
    prefix: str
    multivariants: dict[str, str]
    """Profile name -> HLS multivariant text, for every applicable HLS profile."""
    media: dict[str, str]
    """Child playlist URI (relative to ``prefix``) -> text, for every child of the ``hls`` multivariant."""

    @property
    def main(self) -> str:
        return self.multivariants["hls"]

    def video(self) -> list[tuple[str, VideoRendition]]:
        """(media playlist URI, rendition) of every variant of ``main``, in order."""
        by_modifier = {r.name_modifier: r for r in self.ladder.video}
        return [(v.uri, by_modifier[_modifier(v.uri)]) for v in parse_multivariant(self.main)]


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
    rewritten = [*patch_av1_inits(store, encode), *normalize(store, encode)]
    kind = AssetKind(asset["kind"])
    result = Finalized(
        duration_ms=duration_ms(encode.media[encode.video()[0][0]]),
        break_offsets_ms=break_offsets(encode, asset.get("cue_points_ms", [])) if kind is AssetKind.CONTENT else [],
        signature=[list(row) for row in ladder_signature(encode.main)],
        rewritten=rewritten,
    )
    if kind is AssetKind.AD:
        dash = _dash_profile(ladder, package_profiles)
        return replace(result, creative=sgai_copy(store, encode, asset["asset_id"], content_base_url, dash))
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
    """Read the manifests of an encode, checking completeness and ladder signatures."""
    profiles = applicable(ladder)
    if missing := [p.manifest_key(prefix) for p in profiles if not store.exists(p.manifest_key(prefix))]:
        raise InvalidOutputsError(f"missing manifests: {', '.join(missing)}")
    multivariants = {p.name: _text(store, p.manifest_key(prefix)) for p in profiles if p.format is ManifestFormat.HLS}
    for p in profiles:
        if p.name in multivariants and ladder_signature(multivariants[p.name]) != expected_signature(ladder, p):
            raise InvalidOutputsError(f"{p.manifest_key(prefix)} does not have the variants of {ladder.version}")
    media = {uri: _text(store, f"{prefix}{uri}") for uri in child_uris(multivariants["hls"])}
    return Encode(ladder=ladder, prefix=prefix, multivariants=multivariants, media=media)


def child_uris(multivariant: str) -> list[str]:
    """Every playlist a multivariant references: variants, then renditions (audio) and I-frame playlists."""
    tagged = (attributes(line) for line in multivariant.splitlines() if line.startswith(_CHILD_TAGS))
    uris = [v.uri for v in parse_multivariant(multivariant)] + [a["URI"] for a in tagged if "URI" in a]
    return list(dict.fromkeys(uris))


def patch_av1_inits(store: Store, encode: Encode) -> list[str]:
    """Rewrite the av1C of every AV1 init segment that does not match its rung; returns the keys rewritten."""
    rewritten = []
    fps = encode.ladder.frame_rate.fps
    for uri, r in encode.video():
        if r.codec is not VideoCodec.AV1:
            continue
        key = f"{encode.prefix}{_map_uri(encode.media[uri], uri)}"
        init = store.read(key)
        if init is None:
            raise InvalidOutputsError(f"missing init segment {key}")
        patched = patch_av1c(init, level_idx=av1_level_idx(r.width, r.height, fps), bit_depth=r.bit_depth)
        if patched != init:
            store.write(key, patched)
            rewritten.append(key)
    return rewritten


def normalize(store: Store, encode: Encode) -> list[str]:
    """Nominal bandwidths in every HLS multivariant and I-frame playlist versions; returns the keys rewritten.

    ``encode`` is updated in place so later steps (the SGAI copy) see the rewritten text.
    """
    rewritten = []
    for name, text in encode.multivariants.items():
        fixed = normalize_bandwidth(text, nominal_bandwidths(encode.ladder, text))
        if fixed != text:
            encode.multivariants[name] = fixed
            rewritten.append(_write_text(store, profile(name).manifest_key(encode.prefix), fixed))
    if encode.ladder.options.iframe_playlists:
        for uri, text in encode.media.items():
            if (fixed := bump_iframe_version(text)) != text:
                encode.media[uri] = fixed
                rewritten.append(_write_text(store, f"{encode.prefix}{uri}", fixed))
    return rewritten


def break_offsets(encode: Encode, cue_points_ms: Iterable[int]) -> list[int]:
    """Valid ad-break offsets: segment boundaries common to every track (``encode_timeline``), restricted to the cue
    points if any.

    A cue point maps to the nearest common boundary within half a GOP (ESAM puts it exactly there); points without
    one are dropped with a warning.
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
            logger.warning("cue point is not a segment boundary of every track; dropped", extra={"cue_ms": point})
    return sorted(offsets)


def sgai_copy(store: Store, encode: Encode, asset_id: str, content_base_url: str, dash: RenditionProfile) -> Creative:
    """Write the SGAI-ready manifests of an ad under ``ads/<ladder>/<creative id>/`` (see the module docstring)."""
    base = content_base_url.rstrip("/")
    creative = creative_id(asset_id, encode.ladder.version, content_base_url)
    ad_prefix = f"ads/{ladder_path(encode.ladder.version)}/{creative}/"
    segments = f"{base}/{encode.prefix}"
    for name, text in encode.multivariants.items():
        _write_text(store, f"{ad_prefix}{_file(profile(name))}", text)
    for uri, text in encode.media.items():
        _write_text(store, f"{ad_prefix}{uri}", absolutize_media_playlist(text, segments))
    for p in applicable(encode.ladder):
        if p.format is ManifestFormat.DASH:
            mpd = _text(store, p.manifest_key(encode.prefix))
            _write_text(store, f"{ad_prefix}{_file(p)}", absolutize_mpd(mpd, segments))
    return Creative(
        creative_id=creative,
        ad_prefix=ad_prefix,
        hls_uri=f"{base}/{ad_prefix}{_file(profile('hls'))}",
        dash_uri=f"{base}/{ad_prefix}{_file(dash)}",
    )


def _record(asset: Mapping[str, Any], result: Finalized, *, job_id: str) -> None:
    updated_at = now_iso()
    captions = CaptionPlan.model_validate(asset.get("captions") or {}).provenance()
    values: dict[str, Any] = {
        "status": AssetStatus.ENCODED.value,
        **asset_index(asset["kind"], asset["ladder_version"], AssetStatus.ENCODED, updated_at),
        "duration_ms": result.duration_ms,
        "break_offsets_ms": result.break_offsets_ms,
        "signature": result.signature,
        "captions": [c.model_dump(mode="json", exclude_none=True) for c in captions],
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


def _dash_profile(ladder: Ladder, package_profiles: Sequence[str]) -> RenditionProfile:
    """The DASH profile VAST offers MediaTailor: the first registered one the ladder has, else the first it has
    (every ladder has ``dash-avc``: its first rendition is AVC)."""
    dash = [p for p in applicable(ladder) if p.format is ManifestFormat.DASH]
    return next((p for name in package_profiles for p in dash if p.name == name), dash[0])


def _map_uri(media_playlist: str, uri: str) -> str:
    if (match := _MAP_URI.search(media_playlist)) is None:
        raise InvalidOutputsError(f"{uri} has no EXT-X-MAP")
    return match.group(1)


def _modifier(uri: str) -> str:
    """Rendition NameModifier of a MediaConvert child playlist ``main_<rendition>.m3u8``."""
    return f"_{PurePosixPath(uri).stem.rsplit('_', 1)[-1]}"


def _file(p: RenditionProfile) -> str:
    return PurePosixPath(p.manifest_key("x")).name


def _text(store: Store, key: str) -> str:
    text = read_text(store, key)
    if text is None:
        raise InvalidOutputsError(f"missing {key}")
    return text


def _write_text(store: Store, key: str, text: str) -> str:
    store.write(key, text.encode())
    return key
