import json
import xml.etree.ElementTree as ET

import pytest

from fast_channels.core.demo_demand import (
    DEMO_AD_GOPS,
    DEMO_CREATIVES,
    DemoIndex,
    DemoRole,
    crids,
    demo_index,
    demo_index_key,
    simulator_creatives,
    vast_ad,
)
from fast_channels.core.ladder import Ladder

STANDARD = Ladder.builtin("standard")


def encoded() -> dict[str, tuple[str, str, int]]:
    return {
        c.crid: (
            f"demo-{c.crid}-hash-standard-v1-h",
            f"https://media.example.com/ads/standard/{c.crid}/main.m3u8",
            30030,
        )
        for c in DEMO_CREATIVES
    }


def test_the_demo_creatives() -> None:
    simulator = [c for c in DEMO_CREATIVES if c.role is DemoRole.SIMULATOR]

    assert crids(DemoRole.SIMULATOR) == [f"sim{n}" for n in range(1, 9)]
    assert crids(DemoRole.DIRECT) == ["direct1", "direct2"]
    assert len(crids()) == len({*crids()}) == 10
    # sim3 and sim4 share an advertiser so the pod's separation shows; every other one is distinct
    assert [c.adomain for c in simulator if c.crid in ("sim3", "sim4")] == ["brand-c.example"] * 2
    assert len({c.adomain for c in simulator}) == 7
    assert len({c.adomain for c in DEMO_CREATIVES}) == 9
    assert DEMO_AD_GOPS == 15  # 30.03 s on the 2 s GOP ladder, one stored-request slot


def test_the_index_follows_the_creative_order_and_refuses_strangers() -> None:
    index = demo_index(STANDARD.version, encoded(), width=1920, height=1080)

    assert [c.crid for c in index.creatives] == crids()
    assert DemoIndex.model_validate_json(index.model_dump_json()) == index
    assert demo_index_key(STANDARD.version).endswith("/demo/creatives.json")
    assert demo_index_key(STANDARD.version).startswith("ads/standard")
    partial = demo_index(STANDARD.version, {"sim2": encoded()["sim2"]}, width=1280, height=720)
    assert [(c.crid, c.width) for c in partial.creatives] == [("sim2", 1280)]
    with pytest.raises(ValueError, match="not demo creatives"):
        demo_index(STANDARD.version, {"house1": ("c", "https://x", 1)}, width=1, height=1)


def test_the_simulator_bids_its_creatives_with_a_vast_ad_each() -> None:
    index = demo_index(STANDARD.version, encoded(), width=1920, height=1080)

    creatives = simulator_creatives(index, beacon_url="https://media.example.com/ads/b.gif")

    assert [c["crid"] for c in creatives] == crids(DemoRole.SIMULATOR)
    sim3 = creatives[2]
    assert (sim3["adomain"], sim3["price"], sim3["dur"], sim3["w"], sim3["h"]) == (
        ["brand-c.example"],
        35.0,
        30,
        1920,
        1080,
    )
    assert sim3["burl"] == "https://media.example.com/ads/b.gif?seat=amt&cr=sim3&p=${AUCTION_PRICE}&e=burl"
    json.dumps(creatives)  # the overlay reads it as JSON
    ad = ET.fromstring(sim3["adm"])
    assert ad.get("version") == "4.2"
    assert ad.findtext("Ad/InLine/AdTitle") == "sim3"
    assert ad.find("Ad/InLine/Impression") is None, "the module adds the impressions"
    assert ad.findtext("Ad/InLine/Creatives/Creative/Linear/Duration") == "00:00:30.030"
    [creative] = ad.findall("Ad/InLine/Creatives/Creative")
    assert creative.get("id") == "demo-sim3-hash-standard-v1-h", "MediaTailor caches the preconditioned ad by it"
    media = ad.find("Ad/InLine/Creatives/Creative/Linear/MediaFiles/MediaFile")
    assert media is not None
    assert (media.get("delivery"), media.get("type")) == ("streaming", "application/x-mpegURL")
    assert (media.text or "").strip() == "https://media.example.com/ads/standard/sim3/main.m3u8"


def test_an_advertiser_name_is_escaped() -> None:
    [creative] = demo_index(STANDARD.version, {"sim1": encoded()["sim1"]}, width=1, height=1).creatives
    odd = creative.model_copy(update={"advertiser": "A & B <c>"})

    assert ET.fromstring(vast_ad(odd)).findtext("Ad/InLine/Advertiser") == "A & B <c>"
