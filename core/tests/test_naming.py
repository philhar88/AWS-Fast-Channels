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
    assert naming.is_agent_owned(name, "fast")
    assert not naming.is_agent_owned("fast-kids", "fast")


def test_ladder_key_host_hash_and_creative_id() -> None:
    assert naming.ladder_key("standard@0123abcd45") == "standard_0123abcd45"
    assert naming.host_hash("https://D1.cloudfront.net/x") == naming.host_hash("https://d1.cloudfront.net/y")
    creative = naming.creative_id("house-30", "standard@0123abcd45", "https://d1.cloudfront.net")
    assert creative == f"house-30-standard_0123abcd45-{naming.host_hash('https://d1.cloudfront.net')}"
    assert creative != naming.creative_id("house-30", "standard@0123abcd45", "https://media.example.com")
