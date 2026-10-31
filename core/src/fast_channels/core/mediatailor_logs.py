"""MediaTailor vended-log record fields: the service's defaults and the ones the deployment never stores.

The ad decision server and manifest logs carry fields that must never be kept (DESIGN §5.7): the headers MediaTailor
sent the ad decision server (the viewer's address and user agent; on monetized playback configurations the origin
secret and the playback ticket), the opt-in raw request body, the viewer's request path with its query, and the
session-initialization hook's input and HTTP exchange. Every delivery of those logs (the lake's S3 copies, the release
module's log group) selects :data:`ADS_LOG_FIELDS` or :data:`MANIFEST_LOG_FIELDS`, which leave them out.
"""

from typing import Final

# Default record fields of MediaTailor's deliveries, in the service's order; S3 and CloudWatch Logs list the same
# (``aws logs describe-configuration-templates --service mediatailor``, us-east-1, 2026-10-29).
_ADS_DEFAULT_FIELDS: Final = (
    "eventTimestamp",
    "awsAccountId",
    "originId",
    "customerId",
    "eventType",
    "sessionType",
    "requestId",
    "eventDescription",
    "sessionId",
    "vodVastResponseTimeOffset",
    "adsRequestUrl",
    "adMarkers",
    "segmentationUpid",
    "segmentationTypeId",
    "originalTargetUrl",
    "updatedTargetUrl",
    "rawAdsRequest",
    "rawAdsRequestIndex",
    "rawAdsResponse",
    "rawAdsResponseIndex",
    "avail",
    "vastResponse",
    "vastAd",
    "vodCreativeOffsets",
    "requestHeaders",
    "beaconInfo",
    "adBreakCorrelationId",
    "prefetchScheduleName",
    "eventId",
    "functionId",
    "executionTimeMs",
    "functionType",
    "status",
    "errorType",
    "error",
    "additionalInfo",
    "cause",
    "input",
    "output",
    "http",
    "streamId",
)
_MANIFEST_DEFAULT_FIELDS: Final = (
    "eventTimestamp",
    "awsAccountId",
    "originId",
    "customerId",
    "eventType",
    "sessionType",
    "requestId",
    "eventDescription",
    "sessionId",
    "originRequestUrl",
    "mediaTailorPath",
    "responseBody",
    "requestNextToken",
    "assetPath",
    "originFullUrl",
    "originPrefixUrl",
    "additionalInfo",
    "cause",
    "response",
    "httpCode",
    "errorMessage",
    "adAdsResponse",
    "adAdsRawResponse",
    "adAdsRequest",
    "adNumNewAvails",
    "generatedMediaPlaylist",
    "requestStartTime",
    "requestEndTime",
    "requestStartTimeEpochMillis",
    "requestEndTimeEpochMillis",
    "eventId",
    "functionId",
    "executionTimeMs",
    "functionType",
    "status",
    "errorType",
    "input",
    "output",
    "http",
    "streamId",
)
EXCLUDED_ADS_LOG_FIELDS: Final = frozenset({"requestHeaders", "rawAdsRequest"})
"""Never stored: the headers MediaTailor sent the ADS (viewer address and user agent; on monetized
configurations the origin secret and the ticket) and the opt-in raw request body (the ticket)."""
EXCLUDED_MANIFEST_LOG_FIELDS: Final = frozenset({"mediaTailorPath", "input", "http"})
"""Never stored: the viewer's request path with its query, and the session-initialization hook's input and HTTP
exchange."""
ADS_LOG_FIELDS: Final = tuple(f for f in _ADS_DEFAULT_FIELDS if f not in EXCLUDED_ADS_LOG_FIELDS)
"""Record fields of every delivery of the ad decision server logs."""
MANIFEST_LOG_FIELDS: Final = tuple(f for f in _MANIFEST_DEFAULT_FIELDS if f not in EXCLUDED_MANIFEST_LOG_FIELDS)
"""Record fields of every delivery of the manifest logs."""
VENDED_LOG_TYPES: Final = {
    "AD_DECISION_SERVER_LOGS": "ads",
    "MANIFEST_SERVICE_LOGS": "manifest",
    "TRANSCODE_LOGS": "transcode",
}
"""MediaTailor vended-log types -> delivery source name suffix (``<playback configuration>-<suffix>``)."""
RECORD_FIELDS: Final = {"AD_DECISION_SERVER_LOGS": ADS_LOG_FIELDS, "MANIFEST_SERVICE_LOGS": MANIFEST_LOG_FIELDS}
"""Record fields every delivery of a log type selects; the transcode logs hold nothing to leave out."""
