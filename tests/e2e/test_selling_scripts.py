"""The deploy script's agentic-selling part (``scripts/_selling.py``): pins, build contexts and the stack's context.
Offline (not marked ``e2e``)."""

from pathlib import Path
from typing import Any

import _selling
import pytest
from _demand import context_digest

from fast_channels.core.selling.names import Image


def test_the_pins_name_upstream_commits_and_base_digests() -> None:
    pinned = _selling.pin()

    assert len(pinned["seller"]["commit"]) == len(pinned["guidance"]["commit"]) == 40
    for agent in ("buyer", "governance"):
        assert pinned["guidance"][agent]["digest"].startswith("sha256:")


@pytest.mark.parametrize("image", list(Image))
def test_each_image_builds_from_its_pin(image: Image) -> None:
    variables = _selling.build_variables(image, _selling.pin())

    assert variables["IMAGE"] == image.value
    assert variables["SOURCE_COMMIT"] == (_selling.pin()["seller" if image is Image.SELLER else "guidance"]["commit"])
    assert ("SOURCE_PATH" in variables) is (image is not Image.SELLER)


def test_a_build_context_holds_the_script_the_pins_and_the_sellers_bridge(tmp_path: Path) -> None:
    seller = _selling.build_context(Image.SELLER, tmp_path / "seller")
    buyer = _selling.build_context(Image.BUYER, tmp_path / "buyer")

    assert {p.name for p in seller.iterdir()} == {"build-image.sh", "PIN.toml", "seller"}
    assert (seller / "seller" / "fast" / "bridge.py").is_file()
    assert {p.name for p in buyer.iterdir()} == {"build-image.sh", "PIN.toml"}
    assert context_digest(seller) != context_digest(buyer), "the bridge names the seller's image"
    assert context_digest(buyer) == context_digest(_selling.build_context(Image.BUYER, tmp_path / "again"))


class Ec2:
    def __init__(self, lists: list[dict[str, Any]]) -> None:
        self.lists = lists

    def describe_managed_prefix_lists(self, **_: Any) -> dict[str, Any]:
        return {"PrefixLists": self.lists}


class Stage:
    region = "us-east-1"

    def __init__(self, ec2: Ec2) -> None:
        self.ec2 = ec2

    def client(self, service: str) -> Ec2:
        assert service == "ec2"
        return self.ec2


def test_the_origin_facing_prefix_list_is_looked_up() -> None:
    stage: Any = Stage(Ec2([{"PrefixListId": "pl-3b927c52"}]))
    assert _selling.origin_facing_prefix_list(stage) == "pl-3b927c52"
    with pytest.raises(RuntimeError, match="no managed prefix list"):
        _selling.origin_facing_prefix_list(Stage(Ec2([])))  # type: ignore[arg-type]
