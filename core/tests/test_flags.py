"""The ``flags`` profile: a valid ``AWS.AppConfig.FeatureFlags`` document, read back from the simplified form."""

import re
from typing import Any

import pytest

from fast_channels.core.flags import DemandMode, Flags, OpsSwitches, RecsStrategy, flags_document

KEY = re.compile(r"^[a-z][a-zA-Z\d_-]{0,63}$")


def simplified(document: dict[str, Any]) -> dict[str, Any]:
    """What ``GetLatestConfiguration`` returns: values only, and a disabled flag without its attributes."""
    return {name: value if value["enabled"] else {"enabled": False} for name, value in document["values"].items()}


def test_the_document_follows_the_feature_flags_schema() -> None:
    document = flags_document(Flags())

    assert set(document) == {"version", "flags", "values"}
    assert document["version"] == "1"
    assert document["flags"].keys() == document["values"].keys()
    for name, definition in document["flags"].items():
        assert KEY.match(name), name
        assert set(definition) <= {"name", "description", "attributes"}
        assert len(definition["name"]) <= 64
        attributes = definition.get("attributes", {})
        assert len(attributes) <= 25
        for attribute, spec in attributes.items():
            assert KEY.match(attribute), attribute
            assert spec["constraints"]["type"] in {"string", "boolean"}
            assert not {"pattern", "enum"} <= spec["constraints"].keys()
            assert attribute in document["values"][name]
        assert isinstance(document["values"][name]["enabled"], bool)


def test_every_runbook_switch_is_an_attribute_of_ops() -> None:
    attributes = flags_document(Flags())["flags"]["ops"]["attributes"]

    assert set(attributes) == set(OpsSwitches.model_fields)


@pytest.mark.parametrize(
    "flags",
    [
        Flags(),
        Flags(demand=DemandMode.DEMO, crawl=True, release_promote=True),
        Flags(agents=False, commercial=True, recs=RecsStrategy.RECOMMENDER, ops=OpsSwitches(act=True, freeze=False)),
    ],
)
def test_readers_see_the_deployed_values(flags: Flags) -> None:
    assert Flags.from_simplified(simplified(flags_document(flags))) == flags


def test_a_disabled_demand_flag_means_mode_off() -> None:
    assert Flags.from_simplified({"demand": {"enabled": False}}).demand is DemandMode.OFF
    assert Flags.from_simplified({"demand": {"enabled": True, "mode": "live"}}).demand is DemandMode.LIVE


def test_missing_or_malformed_flags_keep_the_defaults() -> None:
    read = Flags.from_simplified({"ops": "not a flag", "unknown": {"enabled": True}})

    assert read == Flags()
    assert read.ops.act is False  # every runbook runs its dry-run path


def test_a_disabled_ops_flag_keeps_the_safe_defaults() -> None:
    assert Flags.from_simplified({"ops": {"enabled": False}}).ops == OpsSwitches()
