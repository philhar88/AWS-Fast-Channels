"""ESAM signal conditioning for editorial cue points (mediaconvert.md §8.2).

Each offset becomes a zero-duration placement opportunity (SCTE-35 time_signal, segmentation type 0x34) that
forces an IDR and a segment boundary on every track. Offsets must already sit on the GOP grid: AV1 and audio
ignore off-grid points, which truncates the DASH MPD (§8.3). Only SccXml is emitted: manifest conditioning
(MccXml cue tags) was never verified with Channel Assembly, which takes breaks from the schedule (§8.4).

The document is the one verified in the golden job, byte for byte; every interpolated value is an integer.
"""

from collections.abc import Sequence
from typing import Any, Final

PLACEMENT_OPPORTUNITY_START: Final = 52  # segmentation_type_id 0x34

_HEADER: Final = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<SignalProcessingNotification xmlns="urn:cablelabs:iptvservices:esam:xsd:signal:1"'
    ' xmlns:sig="urn:cablelabs:md:xsd:signaling:3.0" xmlns:common="urn:cablelabs:iptvservices:esam:xsd:common:1"'
    ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" acquisitionPointIdentity="fastv3">'
    '<common:BatchInfo batchId="1"><common:Source xsi:type="content:MovieType"/></common:BatchInfo>'
)
_SIGNAL: Final = (
    '<ResponseSignal acquisitionPointIdentity="fastv3" acquisitionSignalID="{n}" signalPointID="{n}"'
    ' action="create"><sig:NPTPoint nptPoint="{t}"/><sig:SCTE35PointDescriptor spliceCommandType="06">'
    '<sig:SegmentationDescriptorInfo segmentEventId="{n}" segmentTypeId="{type}"/>'
    "</sig:SCTE35PointDescriptor></ResponseSignal>"
)
_CONDITIONING: Final = '<ConditioningInfo acquisitionSignalIDRef="{n}" startOffset="PT{t}S" duration="PT0S"/>'


def _seconds(ms: int) -> str:
    return f"{ms // 1000}.{ms % 1000:03d}"


def signal_processing_notification(offsets_ms: Sequence[int]) -> str:
    """One-line ESAM SignalProcessingNotification XML with a placement opportunity at each offset."""
    points = [(n, _seconds(ms)) for n, ms in enumerate(offsets_ms, start=1)]
    signals = "".join(_SIGNAL.format(n=n, t=t, type=PLACEMENT_OPPORTUNITY_START) for n, t in points)
    conditioning = "".join(_CONDITIONING.format(n=n, t=t) for n, t in points)
    return f"{_HEADER}{signals}{conditioning}</SignalProcessingNotification>"


def build_esam(offsets_ms: Sequence[int]) -> dict[str, Any]:
    """MediaConvert ``Settings.Esam`` for GOP-snapped ``offsets_ms`` (positive, sorted and de-duplicated here)."""
    offsets = sorted(set(offsets_ms))
    if not offsets or offsets[0] <= 0:
        raise ValueError("ESAM needs at least one positive offset")
    # ResponseSignalPreroll defaults to 4000 ms, which can put the SCTE-35 a segment early (§8.2).
    return {
        "ResponseSignalPreroll": 0,
        "SignalProcessingNotification": {"SccXml": signal_processing_notification(offsets)},
    }
