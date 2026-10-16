"""Stop the channel (Channel Assembly bills every running hour); the session teardown stops it too, as a guard."""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from harness import Channel, Tools

pytestmark = pytest.mark.e2e


def test_stop_channel(tools: Tools, channel: Channel) -> None:
    assert tools("channels___stop_channel", channel=channel.name)["state"] == "STOPPED"
    listed = {c["name"]: c["state"] for c in tools("channels___list_channels")["channels"]}
    assert listed.get(channel.name) == "STOPPED", listed
