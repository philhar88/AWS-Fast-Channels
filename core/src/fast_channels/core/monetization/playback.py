"""Monetized playback configurations (siblings) and their templated OpenRTB POST to Prebid Server (DESIGN §6.2-6.3).

Each monetized channel gets sibling configurations ``<prefix>-<ch>-p-<s>``: the pair ``a``/``b`` swaps stable and
idle on every promotion (kids profiles get ``k``/``l`` with M4) and ``z`` is the house-only probe. They differ from
the v3.0.0 configuration of the channel, which stays untouched, in:

- the ADS: a POST to the Prebid Server CloudFront, its query carrying ``evid``/``upid``/``dur`` for joins, its body
  only a constant stored-request id built from ``{{session.avail_duration_secs}}`` (MediaTailor inserts values
  without JSON escaping, and an invalid body silently skips the ADS call, P0-20 §4.14), every other value in headers;
- ``PersonalizationThresholdSeconds`` 121, so a partial pod keeps its ads and the break slate plays the rest
  (P0-07, P0-20 §4.11);
- ``FOLLOW_AD_SEQUENCE``, so the module's standalone ads replace a failed sequenced ad in its slot (ad buffet,
  P0-09, P0-20 §4.2);
- ``AvailSuppression`` ``BEHIND_LIVE_EDGE`` ``00:00:00`` as defence in depth (the PRE_SESSION constant is the control);
- the Function mapping.

The bundle holds each sibling's configuration without what only the apply knows: the origin secret (Secrets
Manager), the release (``X-Fast-Fv``, FunctionIds) and the change tags. :func:`render` adds them.
"""

import re
from copy import deepcopy
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import Field, SecretStr, SerializationInfo, SerializerFunctionWrapHandler, model_serializer

from fast_channels.core import specs
from fast_channels.core.monetization.functions import API_CONTEXT, ApiShape, Hook, release_tag, released_id
from fast_channels.core.naming import MEDIATAILOR_NAME_MAX
from fast_channels.core.tags import TAG_APP, TAG_OWNER, tag_key

PERSONALIZATION_THRESHOLD_SECONDS: Final = 121
ADS_REQUEST_TIMEOUT_MS: Final = 2000
LIVE_MAXIMUM_MS: Final = 6000
VOD_MAXIMUM_MS: Final = 6000
PBS_AUCTION_PATH: Final = "/openrtb2/auction"
ADS_QUERY: Final = "evid=[scte.event_id]&upid=[scte.segmentation_upid]&dur=[session.avail_duration_ms]"
"""Join keys in ``MAKING_ADS_REQUEST.adsRequestUrl``; never the ticket (P0-05)."""
BODY_VARIABLES: Final = frozenset({"session.uuid", "session.avail_duration_secs", "scte.event_id"})
"""The only variables the POST body may use: MediaTailor generates them, so they cannot hold a quote."""

HEADER_ORIGIN: Final = "X-Fast-Origin"
HEADER_CONFIGURATION: Final = "X-Fast-Pc"
HEADER_RELEASE: Final = "X-Fast-Fv"
HEADER_SESSION: Final = "X-Fast-Sid"
HEADER_TICKET: Final = "X-Fast-T"
HEADER_PROBE: Final = "X-Fast-Probe"
HEADER_BUFFET: Final = "X-Fast-Buffet"
"""``X-Fast-Buffet: 0`` tells the module to send no standalone ads; never on a ``FOLLOW_AD_SEQUENCE`` sibling, where
the standalones are the per-slot fallback for a first-sight creative (ad buffet on, P0-20 §4.2)."""
AD_SERVER_TARGETING: Final = ("hb_pb", "hb_uuid", "hb_bidder")
"""The Prebid targeting an ad-server sibling's PRE_ADS auction commits to ``player_params.<key>`` for its ADS URL
(M8-09); the only player parameters an ADS URL may carry."""
TICKET_TEMPLATE: Final = "{{player_params.t}}"
TICKET_LOG_EVENTS: Final = frozenset({"RAW_ADS_REQUEST", "PRE_SESSION_INIT_FUNCTION_COMPLETED"})
"""Opt-in log events that carry the ticket; never enabled on a configuration that forwards one (DESIGN §5.7)."""


class SiblingRole(StrEnum):
    """What a sibling configuration serves."""

    GENERAL = "general"
    """The stable/idle pair ``a``/``b`` of every monetized channel."""
    KIDS = "kids"
    """The kids pair ``k``/``l`` (M4): kids stored requests, ``regs.coppa=1``."""
    PROBE = "probe"
    """``z``: house only, its decision records flagged; for canaries and smoke tests."""


ROLE_LETTERS: Final = {SiblingRole.GENERAL: ("a", "b"), SiblingRole.KIDS: ("k", "l"), SiblingRole.PROBE: ("z",)}


class AdsRoute(StrEnum):
    """Which ADS a sibling's breaks go to."""

    PBS = "pbs"
    """The templated OpenRTB POST to Prebid Server (Phase 1)."""
    OPAQUE = "opaque"
    """Whole-break routing to an opaque ad server (Phase 5)."""
    AD_SERVER = "ad_server"
    """A GET to an ad server whose Prebid line items redirect to the pod a PRE_ADS auction cached (M8-09)."""


SEQUENCING: Final = {
    AdsRoute.PBS: "FOLLOW_AD_SEQUENCE",
    AdsRoute.OPAQUE: "IGNORE_AD_SEQUENCE",
    AdsRoute.AD_SERVER: "FOLLOW_AD_SEQUENCE",
}
"""``FOLLOW`` keeps the module's per-slot standalones (also in a cached pod an ad server's Wrapper leads to); ``FOLLOW``
would drop an unsequenced opaque pod (P0-20 §4.3)."""

STORED_REQUEST_PREFIX: Final = {False: "fast-pod-", True: "fast-pod-kids-"}
"""Stored-request id prefix, by whether the sibling serves kids (the kids requests hard-code ``regs.coppa=1``)."""


RELEASE_OWNER: Final = "release-controller"
"""``fast:owner`` of what the Release Controller creates (Functions, sibling configurations). It is not a media-library
owner (``core.tags.Owner``): no library item ever has it."""


def release_tags(prefix: str, *, release: int, change_id: str) -> dict[str, str]:
    """Tags of a Function or sibling configuration created by a release: ``fast:app``, ``fast:owner``,
    ``fast:release`` and ``fast:change`` (the change chain: ``change_id`` → ``rNNNN`` → configuration)."""
    return {
        TAG_APP: prefix,
        TAG_OWNER: RELEASE_OWNER,
        tag_key("release"): release_tag(release),
        tag_key("change"): change_id,
    }


def sibling_name(channel: str, letter: str) -> str:
    """``<channel>-p-<letter>``: the sibling configuration's name (validator R-P6)."""
    if not re.fullmatch(r"[a-z0-9]", letter):
        raise ValueError(f"a sibling letter is one of [a-z0-9], got {letter!r}")
    name = f"{channel}-p-{letter}"
    if len(name) > MEDIATAILOR_NAME_MAX:
        raise ValueError(f"{name!r} is longer than {MEDIATAILOR_NAME_MAX} characters")
    return name


def pbs_ads_url(pbs_origin: str) -> str:
    """The PBS auction URL behind the guidance's CloudFront (``https://<pbs-cdn>``)."""
    return f"{pbs_origin.rstrip('/')}{PBS_AUCTION_PATH}?{ADS_QUERY}"


def ads_body(*, kids: bool) -> str:
    """The POST body: the stored request for the break's length, nothing from the player (DESIGN §6.3).

    PBS generates the request id and ``source.tid``; joins use ``X-Fast-Sid`` and the query's ``evid``.
    """
    stored = f"{STORED_REQUEST_PREFIX[kids]}{{{{session.avail_duration_secs}}}}s"
    return f'{{"ext":{{"prebid":{{"storedrequest":{{"id":"{stored}"}}}}}}}}'


def ads_headers(configuration: str, *, probe: bool, ticketed: bool) -> dict[str, str]:
    """The POST headers the bundle holds; :func:`render` adds the origin secret and the release.

    ``ticketed`` forwards the playback ticket (M4; before it every session is CONTEXTUAL): an unset player
    parameter becomes an empty header, never a literal template (P0-20 §4.6).
    """
    headers = {
        "Content-Type": "application/json",
        HEADER_CONFIGURATION: configuration,
        HEADER_SESSION: "{{session.uuid}}",
    }
    if ticketed:
        headers[HEADER_TICKET] = TICKET_TEMPLATE
    if probe:
        headers[HEADER_PROBE] = "1"
    return headers


# --- the PutPlaybackConfiguration shape ----------------------------------------------------------------------------


class HttpRequest(ApiShape):
    """``AdDecisionServerConfiguration.HttpRequest``."""

    method: Literal["GET", "POST"] | None = None
    body: str | None = None
    headers: dict[str, str] | None = None
    compress_request: Literal["NONE", "GZIP"] | None = None


class VastResponse(ApiShape):
    """``AdDecisionServerConfiguration.VastResponse``."""

    ad_sequencing_mode: (
        Literal[
            "FOLLOW_AD_SEQUENCE", "IGNORE_AD_SEQUENCE", "FOLLOW_AD_SEQUENCE_ONLY_LIVE", "FOLLOW_AD_SEQUENCE_ONLY_VOD"
        ]
        | None
    ) = None


class AdDecisionServerConfiguration(ApiShape):
    http_request: HttpRequest | None = None
    vast_response: VastResponse | None = None


class CdnConfiguration(ApiShape):
    ad_segment_url_prefix: str | None = None
    content_segment_url_prefix: str | None = None


class AdConditioningConfiguration(ApiShape):
    streaming_media_file_conditioning: Literal["TRANSCODE", "NONE"]


class AvailSuppression(ApiShape):
    mode: Literal["OFF", "BEHIND_LIVE_EDGE", "AFTER_LIVE_EDGE"] | None = None
    value: str | None = None
    fill_policy: Literal["FULL_AVAIL_ONLY", "PARTIAL_AVAIL"] | None = None


class AdsPersonalizationTimeouts(ApiShape):
    ads_request_timeout_milliseconds: int | None = None
    live_maximum_ads_personalization_time_milliseconds: int | None = None
    vod_maximum_ads_personalization_time_milliseconds: int | None = None


class AdsInteractionLog(ApiShape):
    publish_opt_in_event_types: tuple[str, ...] | None = None
    exclude_event_types: tuple[str, ...] | None = None


class ManifestServiceInteractionLog(ApiShape):
    publish_opt_in_event_types: tuple[str, ...] | None = None
    exclude_event_types: tuple[str, ...] | None = None


class LogConfiguration(ApiShape):
    """``ConfigureLogsForPlaybackConfiguration`` (the API sets logging with its own call)."""

    percent_enabled: int = Field(ge=0, le=100)
    enabled_logging_strategies: tuple[Literal["VENDED_LOGS", "LEGACY_CLOUDWATCH"], ...]
    ads_interaction_log: AdsInteractionLog | None = None
    manifest_service_interaction_log: ManifestServiceInteractionLog | None = None


class PlaybackConfiguration(ApiShape):
    """A sibling's ``PutPlaybackConfiguration`` request plus its ``LogConfiguration``, in API shape.

    Members a monetized sibling must not set (``SlateAdUrl``: the break slate plays; ``LivePreRollConfiguration``,
    ``YieldOptimizationConfiguration``, ``Bumper``, ``TranscodeProfileName``, ...) are not modelled, so they are
    refused.
    """

    name: str
    video_content_source_url: str
    ad_decision_server_url: str
    ad_decision_server_configuration: AdDecisionServerConfiguration | None = None
    insertion_mode: Literal["STITCHED_ONLY", "PLAYER_SELECT"] | None = None
    cdn_configuration: CdnConfiguration | None = None
    ad_conditioning_configuration: AdConditioningConfiguration | None = None
    personalization_threshold_seconds: int | None = Field(default=None, ge=1)
    avail_suppression: AvailSuppression | None = None
    ads_personalization_timeouts: AdsPersonalizationTimeouts | None = None
    function_mapping: dict[Hook, str] | None = None
    log_configuration: LogConfiguration
    tags: dict[str, str] | None = None
    ads_origin: bool | None = None
    """Bundle only: whether the ADS request carries the origin secret, which :func:`render` adds. ``False`` for a
    third-party ad server (``ad_server`` ``gam`` or ``freewheel``), which must never receive it."""

    @model_serializer(mode="wrap")
    def _request(self, handler: SerializerFunctionWrapHandler, info: SerializationInfo) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if info.context and info.context.get(API_CONTEXT):
            data.pop("AdsOrigin", None)
        return data

    @property
    def sends_origin(self) -> bool:
        """Whether :func:`render` adds the origin secret and the release to the ADS request."""
        return self.ads_origin is not False

    @property
    def headers(self) -> dict[str, str]:
        """The ADS POST headers (empty when unset)."""
        request = self.ad_decision_server_configuration and self.ad_decision_server_configuration.http_request
        return dict(request.headers or {}) if request else {}

    @property
    def body(self) -> str | None:
        """The ADS POST body template."""
        request = self.ad_decision_server_configuration and self.ad_decision_server_configuration.http_request
        return request.body if request else None

    @property
    def ad_sequencing_mode(self) -> str | None:
        response = self.ad_decision_server_configuration and self.ad_decision_server_configuration.vast_response
        return response.ad_sequencing_mode if response else None

    @property
    def opt_in_events(self) -> frozenset[str]:
        """Every opt-in log event the configuration publishes."""
        log = self.log_configuration
        events = [*((log.ads_interaction_log and log.ads_interaction_log.publish_opt_in_event_types) or ())]
        manifest = log.manifest_service_interaction_log
        events += (manifest and manifest.publish_opt_in_event_types) or ()
        return frozenset(events)


def sibling_playback_configuration(
    *,
    name: str,
    video_content_source_url: str,
    content_base_url: str,
    pbs_origin: str,
    log_percent: int,
    function_mapping: dict[Hook, str],
    kids: bool = False,
    probe: bool = False,
    ticketed: bool = False,
    route: AdsRoute = AdsRoute.PBS,
    lab: bool = False,
    ad_server_url: str | None = None,
    ad_server_origin: bool = True,
) -> PlaybackConfiguration:
    """A sibling's desired configuration, without the secret, the release or tags (DESIGN §6.2).

    It starts from ``specs.playback_configuration``, so a sibling shares everything else with the v3.0.0
    configuration (``STITCHED_ONLY``, preconditioned ads, the three timeouts, the CDN prefixes, vended logs).
    ``function_mapping`` names root stems. ``lab`` siblings publish the PRE_SESSION hook events so the smoke can
    read the committed constants; a ticketed sibling never may (validator R-P2).

    An :attr:`AdsRoute.AD_SERVER` sibling GETs ``ad_server_url`` (the ad server's tag with the Prebid targeting,
    M8-09) with no body; with ``ad_server_origin`` (our own mock) it sends ``X-Fast-Pc`` and, at apply, the origin
    secret, and without it (GAM, FreeWheel) no header of ours at all.
    """
    if (route is AdsRoute.AD_SERVER) != (ad_server_url is not None):
        raise ValueError("an ad-server sibling, and only one, has an ad_server_url")
    base = specs.playback_configuration(
        name=name,
        video_content_source_url=video_content_source_url,
        ads_url=ad_server_url if ad_server_url is not None else pbs_ads_url(pbs_origin),
        content_base_url=content_base_url,
        log_percent=log_percent,
        tags={},
    )
    base.pop("Tags")
    base["PersonalizationThresholdSeconds"] = PERSONALIZATION_THRESHOLD_SECONDS
    request: dict[str, Any] = {
        "Method": "POST",
        "CompressRequest": "NONE",
        "Headers": ads_headers(name, probe=probe, ticketed=ticketed),
        "Body": ads_body(kids=kids),
    }
    if route is AdsRoute.AD_SERVER:
        request = {"Method": "GET", **({"Headers": {HEADER_CONFIGURATION: name}} if ad_server_origin else {})}
        base["AdsOrigin"] = ad_server_origin
    base["AdDecisionServerConfiguration"] = {
        "HttpRequest": request,
        "VastResponse": {"AdSequencingMode": SEQUENCING[route]},
    }
    base["AvailSuppression"] = {"Mode": "BEHIND_LIVE_EDGE", "Value": "00:00:00"}
    base["FunctionMapping"] = dict(function_mapping)
    if lab:
        base["LogConfiguration"]["ManifestServiceInteractionLog"]["PublishOptInEventTypes"] = [
            "PRE_SESSION_INIT_HOOK_SUMMARY",
            "PRE_SESSION_INIT_FUNCTION_COMPLETED",
        ]
    return PlaybackConfiguration.model_validate(base)


def render(
    configuration: PlaybackConfiguration,
    *,
    prefix: str,
    release: int,
    change_id: str,
    origin_secret: SecretStr,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``PutPlaybackConfiguration`` and ``ConfigureLogsForPlaybackConfiguration`` arguments for one release.

    Adds the origin secret (only ever here, at apply), ``X-Fast-Fv``, the release's FunctionIds and the ownership
    tags (``fast:owner=release-controller``, ``fast:release``, ``fast:change``). A configuration whose ADS is a
    third-party ad server (:attr:`PlaybackConfiguration.sends_origin` false) gets neither header.
    """
    tag = release_tag(release)
    api = configuration.api()
    put = {key: deepcopy(value) for key, value in api.items() if key != "LogConfiguration"}
    if configuration.sends_origin:
        headers = put["AdDecisionServerConfiguration"]["HttpRequest"].setdefault("Headers", {})
        headers[HEADER_ORIGIN] = origin_secret.get_secret_value()
        headers[HEADER_RELEASE] = tag
    put["FunctionMapping"] = {hook: released_id(stem, release) for hook, stem in put.get("FunctionMapping", {}).items()}
    put["Tags"] = release_tags(prefix, release=release, change_id=change_id)
    return put, {"PlaybackConfigurationName": configuration.name, **api["LogConfiguration"]}
