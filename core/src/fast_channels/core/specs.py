"""Channel Assembly and MediaTailor resource shapes, shared by CDK (L1 props) and the agent's tools (API kwargs).

Every shape is transcribed from the end-to-end proof (e2e.md §2.6-2.12). API and CloudFormation field names are
identical except where :func:`to_cfn_playback_configuration` says otherwise.
"""

import re
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape, quoteattr

from pydantic import BaseModel, Field

from fast_channels.core.naming import ladder_path

if TYPE_CHECKING:
    from fast_channels.core.profiles import RenditionProfile
    from fast_channels.core.timeline import Plan, PlannedProgram

MANIFEST_WINDOW_SECONDS: Final = 60
_HLS_SETTINGS: Final = {"ManifestWindowSeconds": MANIFEST_WINDOW_SECONDS, "AdMarkupType": ["DATERANGE"]}
UPID_TYPE_MPU: Final = 12  # SegmentationUpidType 0x0C
PLACEMENT_OPPORTUNITY_START: Final = 52  # SegmentationTypeId 0x34
QUARTILE_EVENTS: Final = ("start", "firstQuartile", "midpoint", "thirdQuartile", "complete")

ADS_VARIABLES: Final[frozenset[str]] = frozenset(
    {
        "session.id",
        "session.uuid",
        "session.avail_duration_secs",
        "session.avail_duration_ms",
        "session.client_ip",
        "session.user_agent",
        "session.referer",
        "session.accept_language",
        "avail.index",
        "avail.random",
        "avail.source_content_time_epoch_ms",
        "scte.event_id",
        "scte.avail_num",
        "scte.avails_expected",
        "scte.segmentation_event_id",
        "scte.segmentation_type_id",
        "scte.segmentation_upid",
        "scte.segmentation_upid.assetId",
        "scte.segmentation_upid.cueData.key",
        "scte.segmentation_upid.cueData.value",
        "scte.segmentation_upid.private_data.*",
        "scte.unique_program_id",
        "scte.segment_num",
        "scte.segments_expected",
        "scte.sub_segment_num",
        "scte.sub_segments_expected",
        "scte.delivery_not_restricted_flag",
        "scte.archive_allowed_flag",
        "scte.device_restrictions",
        "scte.no_regional_blackout_flag",
        "player_params.*",
    }
)
"""Documented MediaTailor ADS URL variables (ad-insertion.md §6.4), without brackets; ``.*`` entries are families
(``player_params.<name>``, ``...private_data.<n>``). Unknown variables silently become empty strings."""


def channel_outputs(profiles: Sequence[RenditionProfile]) -> list[dict[str, Any]]:
    """Channel ``Outputs``: one HLS output per profile, ManifestName = SourceGroup = profile name, ad markers as
    ``EXT-X-DATERANGE`` (the markup Channel Assembly writes for live HLS; MediaTailor inserts ads there)."""
    return [
        {"ManifestName": p.name, "SourceGroup": p.name, "HlsPlaylistSettings": deepcopy(_HLS_SETTINGS)}
        for p in profiles
    ]


def http_package_configurations(asset_prefix: str, profiles: Sequence[RenditionProfile]) -> list[dict[str, str]]:
    """VOD source ``HttpPackageConfigurations``; Paths are relative to the source location's BaseUrl."""
    base = asset_prefix.strip("/")
    return [{"Path": f"/{p.manifest_key(base)}", "SourceGroup": p.name, "Type": "HLS"} for p in profiles]


def ad_breaks(
    program: PlannedProgram, *, source_location: str, slate_vod_sources: Mapping[int, str]
) -> list[dict[str, Any]]:
    """CreateProgram ``AdBreaks``: TIME_SIGNAL placement opportunities carrying the event id and MPU UPID.

    ``slate_vod_sources`` maps a break's slate length in GOPs to the VOD source of that slate.
    TIME_SIGNAL (not SPLICE_INSERT) so ``[scte.segmentation_upid]`` reaches the ADS (e2e.md §7).
    """
    total = len(program.breaks)
    return [
        {
            "OffsetMillis": b.offset_ms,
            "MessageType": "TIME_SIGNAL",
            "Slate": {"SourceLocationName": source_location, "VodSourceName": slate_vod_sources[b.slate_gops]},
            "TimeSignalMessage": {
                "SegmentationDescriptors": [
                    {
                        "SegmentationEventId": b.event_id,
                        "SegmentationUpidType": UPID_TYPE_MPU,
                        "SegmentationUpid": b.upid,
                        "SegmentationTypeId": PLACEMENT_OPPORTUNITY_START,
                        "SegmentNum": n,
                        "SegmentsExpected": total,
                    }
                ]
            },
        }
        for n, b in enumerate(program.breaks, start=1)
    ]


def program_requests(
    plan: Plan,
    *,
    source_location: str,
    slate_vod_sources: Mapping[int, str],
    after_program: str | None = None,
    before_program: str | None = None,
) -> list[dict[str, Any]]:
    """CreateProgram requests for a plan, in creation order: every program ``ABSOLUTE`` at its planned start.

    An ``ABSOLUTE`` start is a not-before time: Channel Assembly fills a gap before it with filler slate and truncates
    an overrunning predecessor at a common segment boundary, so every program airs at its planned time (within one
    segment) however late the channel starts. The list order still defines play order (channel-assembly.md §5.3):
    each program goes after its predecessor in the plan, and the first one after ``after_program``, the scheduled
    program that precedes the block in time, or before ``before_program``, the first scheduled program, when
    nothing precedes the block. Give neither on an empty schedule.
    """
    if after_program is not None and before_program is not None:
        raise ValueError("give after_program or before_program, not both")
    position, relative = (
        ("AFTER_PROGRAM", after_program) if before_program is None else ("BEFORE_PROGRAM", before_program)
    )
    requests = []
    for p in plan.programs:
        transition: dict[str, Any] = {
            "Type": "ABSOLUTE",
            "RelativePosition": position,
            "ScheduledStartTimeMillis": p.start_ms,
        }
        if relative is not None:
            transition["RelativeProgram"] = relative
        requests.append(
            {
                "ChannelName": plan.channel,
                "ProgramName": p.name,
                "SourceLocationName": source_location,
                "VodSourceName": p.vod_source,
                "ScheduleConfiguration": {"Transition": transition},
                "AdBreaks": ad_breaks(p, source_location=source_location, slate_vod_sources=slate_vod_sources),
            }
        )
        position, relative = "AFTER_PROGRAM", p.name
    return requests


def channel_policy(*, channel_arn: str, playback_configuration_arn: str, account: str) -> dict[str, Any]:
    """Channel policy letting only the channel's own playback configuration read it (SigV4, no anonymous)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowMediaTailorSsaiSigV4",
                "Effect": "Allow",
                "Principal": {"Service": "mediatailor.amazonaws.com"},
                "Action": "mediatailor:GetManifest",
                "Resource": channel_arn,
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": playback_configuration_arn},
                },
            }
        ],
    }


def playback_configuration(
    *,
    name: str,
    video_content_source_url: str,
    ads_url: str,
    content_base_url: str,
    log_percent: int,
    tags: Mapping[str, str],
) -> dict[str, Any]:
    """Playback configuration in API shape (``GetPlaybackConfiguration`` fields, including ``LogConfiguration``).

    ``STITCHED_ONLY``: server-side ad insertion only. ``AdConditioning NONE`` stitches ads preconditioned on the
    channel ladder (the only exact result in e2e.md §4.1). No SlateAdUrl: MediaTailor would transcode it, and an
    unfilled avail shows the Channel Assembly slate, which is already on the ladder. Use
    :func:`playback_configuration_requests` to call the API.
    """
    if not 0 <= log_percent <= 100:
        raise ValueError("log_percent must be 0-100")
    return {
        "Name": name,
        "VideoContentSourceUrl": video_content_source_url,
        "AdDecisionServerUrl": validate_ads_url(ads_url),
        "InsertionMode": "STITCHED_ONLY",
        "CdnConfiguration": {"AdSegmentUrlPrefix": content_base_url, "ContentSegmentUrlPrefix": content_base_url},
        "AdConditioningConfiguration": {"StreamingMediaFileConditioning": "NONE"},
        "PersonalizationThresholdSeconds": 5,
        # The three timeouts must be set together or PutPlaybackConfiguration fails (e2e.md §8 item 1).
        "AdsPersonalizationTimeouts": {
            "AdsRequestTimeoutMilliseconds": 2000,
            "LiveMaximumAdsPersonalizationTimeMilliseconds": 6000,
            "VodMaximumAdsPersonalizationTimeMilliseconds": 6000,
        },
        "LogConfiguration": {
            "PercentEnabled": log_percent,
            "EnabledLoggingStrategies": ["VENDED_LOGS"],
            "AdsInteractionLog": {"PublishOptInEventTypes": ["RAW_ADS_RESPONSE"]},
            "ManifestServiceInteractionLog": {"ExcludeEventTypes": ["GENERATED_MANIFEST"]},
        },
        "Tags": dict(tags),
    }


def playback_configuration_requests(api: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split :func:`playback_configuration` into PutPlaybackConfiguration and ConfigureLogsForPlaybackConfiguration
    kwargs (the API sets logging with a separate call)."""
    put = {k: deepcopy(v) for k, v in api.items() if k != "LogConfiguration"}
    return put, {"PlaybackConfigurationName": api["Name"], **deepcopy(api["LogConfiguration"])}


def to_cfn_playback_configuration(api: Mapping[str, Any]) -> dict[str, Any]:
    """CloudFormation ``AWS::MediaTailor::PlaybackConfiguration`` properties from the API shape.

    Differences: ``Tags`` is a list of ``{Key, Value}``; ``AdDecisionServerConfiguration.HttpRequest.Method`` is
    ``HttpMethod``.
    """
    cfn = deepcopy(dict(api))
    cfn["Tags"] = [{"Key": k, "Value": v} for k, v in sorted(cfn.get("Tags", {}).items())]
    request = cfn.get("AdDecisionServerConfiguration", {}).get("HttpRequest", {})
    if "Method" in request:
        request["HttpMethod"] = request.pop("Method")
    return cfn


def validate_ads_url(url: str) -> str:
    """Return ``url`` if every ``[...]`` variable in it is in :data:`ADS_VARIABLES`, else raise ValueError."""
    families = [re.escape(v.removesuffix("*")) + r"\w+" for v in ADS_VARIABLES if v.endswith(".*")]
    unknown = [
        v
        for v in re.findall(r"\[([^\]]*)\]", url)
        if v not in ADS_VARIABLES and not any(re.fullmatch(family, v) for family in families)
    ]
    if unknown:
        raise ValueError(f"unknown MediaTailor ADS variables {unknown}")
    return url


def vast_key(ladder_version: str) -> str:
    """S3 key of a ladder's static VAST pod."""
    return f"ads/{ladder_path(ladder_version)}/vast.xml"


def ads_url(*, content_base_url: str, ladder_version: str, channel: str) -> str:
    """ADS URL of a channel: the ladder's VAST pod on the CDN, with correlation ids for logs.

    ``dur_ms`` is frame-accurate (29.97 fps breaks are not whole seconds); ``sid``/``evid``/``upid`` join the
    CloudFront log of the ADS request to the MediaTailor session and the scheduled break (e2e.md §7).
    """
    if urlsplit(content_base_url).scheme != "https":
        raise ValueError("content_base_url must be an absolute https URL")
    url = (
        f"{content_base_url.rstrip('/')}/{vast_key(ladder_version)}?ch={quote(channel, safe='')}"
        "&sid=[session.uuid]&evid=[scte.event_id]&upid=[scte.segmentation_upid]"
        "&dur_ms=[session.avail_duration_ms]"
    )
    return validate_ads_url(url)


class Creative(BaseModel):
    """A preconditioned ad: absolute streaming manifests encoded with the channel ladder."""

    creative_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    duration_ms: int = Field(gt=0)
    hls_uri: str = Field(
        description="Absolute URL on the CDN of the creative's multivariant copy under ads/ (the CDN does not serve "
        "assets/*.m3u8); its media playlists carry absolute segment URIs."
    )
    width: int = 1920
    height: int = 1080


def _duration(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}.{millis:03d}"


def _cdata(url: str) -> str:
    return f"<![CDATA[{url}]]>"  # '&' inside CDATA must not be escaped (ad-insertion.md §6.2)


def _ad(sequence: int, c: Creative, beacon: str) -> str:
    def track(event: str) -> str:
        return _cdata(f"{beacon}?evt={event}&cr={quote(c.creative_id)}")

    size = f'width="{c.width}" height="{c.height}"'
    tracking = "".join(f'<Tracking event="{e}">{track(e)}</Tracking>' for e in QUARTILE_EVENTS)
    return (
        f'<Ad id={quoteattr(f"ad-{sequence}-{c.creative_id}")} sequence="{sequence}"><InLine>'
        f"<AdSystem>fast-channels</AdSystem><AdTitle>{escape(c.creative_id)}</AdTitle>"
        f"<AdServingId>{escape(c.creative_id)}</AdServingId>"
        f'<Impression id="imp">{track("impression")}</Impression>'
        f'<Creatives><Creative id={quoteattr(c.creative_id)} sequence="1" adId={quoteattr(f"ad-{c.creative_id}")}>'
        f'<UniversalAdId idRegistry="fast-channels">{escape(c.creative_id)}</UniversalAdId>'
        f"<Linear><Duration>{_duration(c.duration_ms)}</Duration><TrackingEvents>{tracking}</TrackingEvents>"
        f"<MediaFiles>"
        f'<MediaFile id={quoteattr(f"{c.creative_id}-hls")} delivery="streaming" type="application/x-mpegURL" '
        f"{size}>{_cdata(c.hls_uri)}</MediaFile>"
        f"</MediaFiles></Linear></Creative></Creatives></InLine></Ad>"
    )


def vast_pod(
    *,
    creatives: Sequence[Creative],
    content_base_url: str,
    fill_ms: int = 0,
    beacon_path: str = "/ads/b.gif",
) -> str:
    """Static VAST 4.2 pod of preconditioned creatives (streaming HLS only).

    The creatives repeat in order until the pod lasts at least ``fill_ms`` (the longest break), because MediaTailor
    fills an avail with the pod's ads in sequence and drops the avail when too much of it would stay unfilled.

    No progressive MP4: with AdConditioning NONE, MediaTailor would transcode it when streaming files cannot fill
    an avail, reintroducing the disproven transcode path (_review.md, SlateAdUrl). Impressions and quartiles hit a
    1x1 GIF on the CDN so they appear in CloudFront logs; VAST macros are not expanded by MediaTailor (e2e.md §7).
    """
    if not creatives:
        raise ValueError("a VAST pod needs at least one creative")
    beacon = f"{content_base_url.rstrip('/')}/{beacon_path.lstrip('/')}"
    pod: list[Creative] = []
    while not pod or sum(c.duration_ms for c in pod) < fill_ms:
        pod.extend(creatives)
    ads = "".join(_ad(n, c, beacon) for n, c in enumerate(pod, start=1))
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<VAST version="4.2" xmlns="http://www.iab.com/VAST">{ads}</VAST>\n'


def playback_urls(*, cdn_base: str, playback_prefix_hls: str, outputs: Sequence[str]) -> dict[str, str]:
    """Viewer URL (SSAI) per channel output, through the CDN.

    ``playback_prefix_hls`` is the playback configuration's HLS ``ManifestEndpointPrefix``.
    """
    cdn = cdn_base.rstrip("/")
    hls_path = urlsplit(playback_prefix_hls).path.rstrip("/")
    return {output: f"{cdn}{hls_path}/{output}.m3u8" for output in outputs}
