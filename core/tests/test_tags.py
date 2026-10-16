from fast_channels.core import tags
from fast_channels.core.tags import Owner


def test_keys_and_owner_values_are_the_deployed_ones() -> None:
    assert (tags.TAG_APP, tags.TAG_OWNER, tags.TAG_LADDER, tags.TAG_CORRELATION) == (
        "fast:app",
        "fast:owner",
        "fast:ladder",
        "fast:correlation-id",
    )
    assert [o.value for o in Owner] == ["iac", "pipeline", "agent"]
    assert set(tags.RUNTIME_OWNERS) == {"pipeline", "agent"}


def test_resource_tags() -> None:
    assert tags.resource_tags("fast", Owner.IAC) == {"fast:app": "fast", "fast:owner": "iac"}
    assert tags.resource_tags(
        "fast", Owner.PIPELINE, ladder_version="standard@0123456789", correlation_id="c1", asset="x", source_uri="s"
    ) == {
        "fast:app": "fast",
        "fast:owner": "pipeline",
        "fast:ladder": "standard@0123456789",
        "fast:correlation-id": "c1",
        "fast:asset": "x",
        "fast:source-uri": "s",
    }
    assert tags.tag_key("asset_id") == "fast:asset-id"


def test_ownership_checks_match_the_prefix_exactly() -> None:
    agent = tags.resource_tags("fast", Owner.AGENT)
    assert tags.owned_by(agent, "fast") and tags.runtime_owned(agent, "fast")
    assert not tags.owned_by(agent, "fastci")  # one deployment never claims another's resources
    assert not tags.runtime_owned(tags.resource_tags("fast", Owner.IAC), "fast")  # CloudFormation deletes those
    assert not tags.runtime_owned({"fast:app": "fast"}, "fast")
