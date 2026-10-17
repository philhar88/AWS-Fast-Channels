"""The stage's stack outputs: the contract the scripts, the console and this suite read (``scripts/_stack.py``)."""

import re

import pytest
from _stack import OUTPUT_KEYS, Plane, Stage

from fast_channels.core.ingest import LADDER_VERSION_PATTERN
from fast_channels.functions.tools.registry import TARGETS

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("plane", list(Plane))
def test_stack_outputs_present(stage: Stage, plane: Plane) -> None:
    outputs = stage.outputs(plane)
    missing = [key for key in OUTPUT_KEYS[plane] if not outputs.get(key)]
    assert not missing, f"{stage.stack_name(plane)} has no (or an empty) {', '.join(missing)}"


def test_stack_outputs_well_formed(stage: Stage) -> None:
    urls = {
        **{key: stage.output(Plane.MEDIA, key) for key in ("CdnBaseUrl", "ContentBaseUrl", "DashboardUrl")},
        **{key: stage.output(Plane.OPERATOR, key) for key in ("ConsoleUrl", "RuntimeInvokeUrl", "GatewayUrl")},
    }
    assert all(url.startswith("https://") for url in urls.values()), urls
    assert re.fullmatch(LADDER_VERSION_PATTERN, stage.output(Plane.MEDIA, "DefaultLadderVersion"))
    assert set(stage.json_output(Plane.OPERATOR, "ToolFunctions")) == set(TARGETS)
    channels = stage.json_output(Plane.MEDIA, "Channels")
    assert channels, "no IaC channel"
    for channel in channels:
        assert {"name", "ladderVersion", "outputs", "playback"} <= channel.keys(), channel
        assert set(channel["playback"]) == set(channel["outputs"]), channel
        # One SSAI viewer URL per output (core.specs.playback_urls), through the CDN.
        cdn = urls["CdnBaseUrl"].rstrip("/")
        for output, url in channel["playback"].items():
            assert isinstance(url, str) and url.startswith(cdn) and url.endswith(f"/{output}.m3u8"), channel
