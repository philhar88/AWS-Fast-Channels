import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from fast_channels.core.esam import build_esam

DATA = Path(__file__).parent / "data"

GOLDEN = json.loads((DATA / "golden-job.json").read_text())["Settings"]["Esam"]


def test_reproduces_the_verified_golden_esam() -> None:
    assert build_esam([40000, 20000, 20000]) == GOLDEN


def test_document_is_well_formed() -> None:
    root = ET.fromstring(build_esam([2002, 4004, 60060])["SignalProcessingNotification"]["SccXml"])
    ns = {"e": "urn:cablelabs:iptvservices:esam:xsd:signal:1", "sig": "urn:cablelabs:md:xsd:signaling:3.0"}
    points = [p.get("nptPoint") for p in root.iterfind("e:ResponseSignal/sig:NPTPoint", ns)]
    assert points == ["2.002", "4.004", "60.060"]
    assert [c.get("startOffset") for c in root.iterfind("e:ConditioningInfo", ns)] == [
        "PT2.002S",
        "PT4.004S",
        "PT60.060S",
    ]


@pytest.mark.parametrize("offsets", [[], [0, 2002], [-2002]])
def test_rejects_non_positive_offsets(offsets: list[int]) -> None:
    with pytest.raises(ValueError, match="positive offset"):
        build_esam(offsets)
