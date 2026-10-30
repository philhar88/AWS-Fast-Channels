import pytest

from fast_channels.core import naming


def test_slug_folds_accents_and_punctuation() -> None:
    assert naming.slug("Big Buck Bunny (Café Edition)!") == "big-buck-bunny-cafe-edition"
    assert naming.slug("a" * 60, max_len=10) == "a" * 10
    assert naming.slug("ab---" + "c" * 10, max_len=3) == "ab"  # no trailing hyphen after truncation
    assert naming.slug("!!!") == ""


def test_mediatailor_name_is_safe_and_bounded() -> None:
    assert naming.mediatailor_name("fast", "demo channel", "", "hls/avc") == "fast-demo-channel-hls-avc"
    long = naming.mediatailor_name("x" * 80)
    assert len(long) == naming.MEDIATAILOR_NAME_MAX
    assert long == naming.mediatailor_name("x" * 80)  # deterministic
    assert long != naming.mediatailor_name("x" * 81)  # the hash keeps truncated names unique
    with pytest.raises(ValueError, match="no valid characters"):
        naming.mediatailor_name("///")


def test_agentcore_name_has_no_hyphens() -> None:
    assert naming.agentcore_name("fast-demo", "scheduler agent") == "fast_demo_scheduler_agent"
    assert len(naming.agentcore_name("a" * 60)) == naming.AGENTCORE_NAME_MAX
    with pytest.raises(ValueError, match="start with a letter"):
        naming.agentcore_name("1fast")


def test_agent_owned_namespace() -> None:
    name = naming.agent_owned_name("fast", "kids")
    assert name == "fast-agent-kids"
    assert naming.agent_namespace("fast") == "fast-agent-"
    assert naming.is_agent_owned(name, "fast")
    assert not naming.is_agent_owned("fast-kids", "fast")
    assert not naming.is_agent_owned("fastci-agent-kids", "fast")


@pytest.mark.parametrize("prefix", ["fast", "fastm", "fastcih", "fastciads", "a1", "a" * 16])
def test_deployment_prefixes(prefix: str) -> None:
    assert naming.check_prefix(prefix) == prefix


@pytest.mark.parametrize(
    ("prefix", "why"),
    [
        ("fast-x", "the grants of a deployment 'fast' ('fast-*') would cover the resources of 'fast-x'"),
        ("fastci-pr7", "the grants of a deployment 'fastci' ('fastci-*') would cover the resources of 'fastci-pr7'"),
        ("Fast", "scoped by '<prefix>-*'"),
        ("f", "use 2-16 lowercase letters and digits"),
        ("a" * 17, "use 2-16 lowercase letters and digits"),
        ("1fast", "starting with a letter"),
        ("fast_m", "without hyphens"),
    ],
)
def test_a_prefix_that_could_fall_inside_another_deployments_grants_is_refused(prefix: str, why: str) -> None:
    with pytest.raises(ValueError, match="is not a deployment prefix") as error:
        naming.check_prefix(prefix)

    assert why in str(error.value)


def test_stage_id() -> None:
    assert naming.stage_id(naming.DEFAULT_PREFIX) == "FastChannels"
    assert naming.stage_id("news") == "FastChannels-news"


def test_runtime_resource_names() -> None:
    version = "standard@0123456789"
    assert naming.job_template_name("fast", version) == "fast-standard_0123456789"
    assert naming.source_location_name("fast") == "fast-media"
    assert naming.vod_source_name("filler-1a2b3c", version) == "filler-1a2b3c-standard_0123456789"
    long = naming.vod_source_name("a" * 48, version)
    assert len(long) == naming.VOD_SOURCE_NAME_MAX and long == naming.vod_source_name("a" * 48, version)


def test_ladder_key_is_a_deprecated_alias_of_ladder_path() -> None:
    with pytest.deprecated_call(match="renamed ladder_path"):
        assert naming.ladder_key("standard@0123abcd45") == naming.ladder_path("standard@0123abcd45")


def test_ladder_path_host_hash_and_creative_id() -> None:
    assert naming.ladder_path("standard@0123abcd45") == "standard_0123abcd45"
    assert naming.host_hash("https://D1.cloudfront.net/x") == naming.host_hash("https://d1.cloudfront.net/y")
    creative = naming.creative_id("house-30", "standard@0123abcd45", "https://d1.cloudfront.net")
    assert creative == f"house-30-standard_0123abcd45-{naming.host_hash('https://d1.cloudfront.net')}"
    assert creative != naming.creative_id("house-30", "standard@0123abcd45", "https://media.example.com")


def test_vod_source_names_fit_fifty_characters() -> None:
    assert naming.vod_source_name("sintel", "standard@046fbb4ccd") == "sintel-standard_046fbb4ccd"
    long = naming.vod_source_name("e2e-pattern-spoken-20261016191837", "standard@046fbb4ccd")
    assert len(long) == naming.VOD_SOURCE_NAME_MAX and long.startswith("e2e-pattern-spoken-20261016191837-standard_")
    agent = naming.agent_vod_source_name("fast", "a" * 48, "standard@046fbb4ccd")
    assert len(agent) == naming.VOD_SOURCE_NAME_MAX and naming.is_agent_owned(agent, "fast")
    assert naming.agent_vod_source_name("fast", "pilot", "standard@046fbb4ccd") == "fast-agent-pilot-046fbb4ccd"
