"""The autonomy envelope v1 (M9): its model, its version and the verdict on one action."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from fast_channels.core.agents.envelope import (
    ALWAYS_ON,
    PROGRAMMED,
    PUBLISH_LEAD_MINUTES,
    AgentEnvelope,
    Envelope,
    Grant,
    Level,
    Refusal,
    Role,
    like,
    scoped_channel,
    verdict,
    window_of,
)

NOW = datetime(2026, 11, 7, 14, 37, 12, tzinfo=UTC)
START = "channels___start_channel"


def resolved(**kwargs: object) -> Envelope:
    return Envelope.model_validate({"always_on": ["{prefix}-demo"], **kwargs}).resolved("fast")


def test_the_default_envelope_lets_only_the_operations_agent_restart_always_on_channels() -> None:
    envelope = resolved()

    assert envelope.always_on == ("fast-demo",)
    assert envelope.enabled() == [Role.OPERATIONS]
    [grant] = envelope.agents[Role.OPERATIONS].grants
    assert (grant.tool, grant.channels, grant.per_hour) == (START, ("fast-demo",), 1)
    assert envelope.agents[Role.SALES].level is Level.L1, "Y2: propose-only before 30 days or 20 buys"
    assert "channels___stop_channel" in envelope.agents[Role.OPERATIONS].needs_human, "R11 stays with a human"


def test_the_version_is_the_canonical_documents_hash_and_changes_with_it() -> None:
    first, again = resolved(), resolved()
    other = Envelope.model_validate({"always_on": ["{prefix}-news"]}).resolved("fast")

    assert first.version == again.version and first.version.startswith("sha256:") and len(first.version) == 71
    assert other.version != first.version
    assert Envelope.model_validate_json(first.document) == first, "the runner reads back what was hashed"


def test_placeholders_expand_and_repeats_collapse() -> None:
    envelope = Envelope(
        always_on=("{prefix}-demo", "{prefix}-demo"),
        agents={
            Role.OPERATIONS: AgentEnvelope(
                enabled=True, grants=(Grant(tool=START, channels=(ALWAYS_ON, "{prefix}-demo", "{prefix}-news")),)
            )
        },
    ).resolved("fastx")

    assert envelope.always_on == ("fastx-demo",)
    assert envelope.agents[Role.OPERATIONS].grants[0].channels == ("fastx-demo", "fastx-news")


@pytest.mark.parametrize(
    ("grant", "message"),
    [
        ({"tool": "catalog___search_content", "channels": ["x"]}, "names no channel"),
        ({"tool": START, "channels": ["x"], "lead_minutes": 120}, "no start the envelope can bound"),
        ({"tool": "channels start"}, "should match pattern"),
    ],
)
def test_a_grant_must_be_consistent(grant: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Grant.model_validate(grant)


def test_an_agent_may_not_be_granted_what_needs_a_human_or_a_tool_twice() -> None:
    with pytest.raises(ValidationError, match="granted and needing a human"):
        AgentEnvelope(grants=(Grant(tool=START, channels=("a",)),), needs_human=(START,))
    with pytest.raises(ValidationError, match="one grant per tool"):
        AgentEnvelope(grants=(Grant(tool=START, channels=("a",)), Grant(tool=START, channels=("b",))))


def test_an_action_inside_the_envelope_names_its_caps() -> None:
    found = verdict(resolved(), Role.OPERATIONS, START, {"channel": "fast-demo"}, now=NOW)

    assert found.allowed and found.channel == "fast-demo" and found.refusal is None
    window = window_of(NOW)
    assert window == datetime(2026, 11, 7, 14, tzinfo=UTC)
    assert [(cap.key, cap.limit, cap.window) for cap in found.caps] == [
        ("CAP#operations", 6, window),
        (f"CAP#operations#{START}#fast-demo", 1, window),
    ]


@pytest.mark.parametrize(
    ("role", "tool", "arguments", "refusal"),
    [
        (Role.PROGRAMMING, "schedule___publish", {"plan_id": "fast-demo.ab"}, Refusal.DISABLED),
        (Role.OPERATIONS, "channels___stop_channel", {"channel": "fast-demo"}, Refusal.NEEDS_HUMAN),
        (Role.OPERATIONS, "channels___delete_channel", {"channel": "fast-agent-x"}, Refusal.NEEDS_HUMAN),
        (Role.OPERATIONS, "schedule___publish", {"plan_id": "fast-demo.ab"}, Refusal.NOT_GRANTED),
        (Role.OPERATIONS, START, {"channel": "fast-news"}, Refusal.OUT_OF_SCOPE),
        (Role.OPERATIONS, START, {}, Refusal.OUT_OF_SCOPE),
    ],
)
def test_an_action_outside_the_envelope_is_refused_with_its_reason(
    role: Role, tool: str, arguments: dict[str, object], refusal: Refusal
) -> None:
    found = verdict(resolved(), role, tool, arguments, now=NOW)

    assert not found.allowed and found.refusal is refusal and not found.caps


def test_a_propose_only_agent_is_refused_every_gated_action() -> None:
    envelope = Envelope(
        agents={
            Role.SALES: AgentEnvelope(
                enabled=True, level=Level.L1, grants=(Grant(tool="schedule___publish", channels=("fast-demo",)),)
            )
        }
    ).resolved("fast")

    found = verdict(envelope, Role.SALES, "schedule___publish", {"plan_id": "fast-demo.ab"}, now=NOW)

    assert not found.allowed and found.refusal is Refusal.PROPOSE_ONLY


def test_scopes_match_as_cedar_like_does() -> None:
    assert like("fast-demo.0a1b", "fast-demo.*") and not like("fast-demox.0a1b", "fast-demo.*")
    assert like("fast-demo", "fast-demo") and not like("fast-demo2", "fast-demo")
    assert not like("fastXdemo", "fast.demo"), "only * is a wildcard"
    assert scoped_channel("schedule___publish", {"plan_id": "fast-news.12"}, ("fast-demo", "fast-news")) == "fast-news"
    assert scoped_channel(START, {"channel": 7}, ("fast-demo",)) is None


def test_without_an_always_on_channel_the_operations_agent_restarts_none() -> None:
    envelope = Envelope().resolved("fast")

    found = verdict(envelope, Role.OPERATIONS, START, {"channel": "fast-demo"}, now=NOW)

    assert not found.allowed and found.refusal is Refusal.OUT_OF_SCOPE and "no channel" in found.reason


PUBLISH = "schedule___publish"


def programming(**kwargs: object) -> Envelope:
    agents = Envelope().agents
    on = {**agents, Role.PROGRAMMING: agents[Role.PROGRAMMING].model_copy(update={"enabled": True})}
    return Envelope(programmed=("{prefix}-demo",), agents=on, **kwargs).resolved("fast")  # type: ignore[arg-type]


def test_an_envelope_with_no_channel_reads_back_what_was_hashed() -> None:
    """Resolved with no always-on or programmed channel, a scoped grant has no channels but keeps its cap: the
    runner, the interceptor and the deploy's record all read the document back (a stage with ``always_on: []``)."""
    envelope = Envelope().resolved("fast")

    assert envelope.agents[Role.OPERATIONS].grants[0].channels == ()
    assert Envelope.model_validate_json(envelope.document) == envelope
    found = verdict(envelope, Role.OPERATIONS, START, {"channel": "fast-demo"}, now=NOW)
    assert not found.allowed and found.refusal is Refusal.OUT_OF_SCOPE, "it permits nothing"


def test_programmed_channels_expand_into_the_programming_agents_grants() -> None:
    envelope = programming()

    assert envelope.programmed == ("fast-demo",)
    plan, publish = envelope.agents[Role.PROGRAMMING].grants
    assert (plan.tool, plan.channels) == ("schedule___plan", ("fast-demo",))
    assert (publish.tool, publish.channels, publish.per_hour, publish.lead_minutes) == (
        PUBLISH,
        ("fast-demo",),
        6,
        PUBLISH_LEAD_MINUTES,
    )
    assert Envelope(programmed=(PROGRAMMED,)).agents[Role.PROGRAMMING].enabled is False, "opt in per stage"


@pytest.mark.parametrize(
    ("starts", "allowed"),
    [
        (None, False),
        (NOW + timedelta(minutes=30), False),
        (NOW + timedelta(minutes=PUBLISH_LEAD_MINUTES - 1), False),
        (NOW + timedelta(minutes=PUBLISH_LEAD_MINUTES), True),
        (NOW + timedelta(hours=5), True),
    ],
)
def test_a_publish_must_start_at_least_its_lead_ahead(starts: datetime | None, allowed: bool) -> None:
    found = verdict(programming(), Role.PROGRAMMING, PUBLISH, {"plan_id": "fast-demo.0a1b"}, now=NOW, starts=starts)

    assert found.allowed is allowed
    if allowed:
        assert [cap.key for cap in found.caps] == ["CAP#programming", f"CAP#programming#{PUBLISH}#fast-demo"]
    else:
        assert found.refusal is Refusal.LEAD and "need a human" in found.reason and found.channel == "fast-demo"


def test_a_publish_on_a_channel_that_is_not_programmed_is_out_of_scope() -> None:
    later = NOW + timedelta(hours=5)

    found = verdict(programming(), Role.PROGRAMMING, PUBLISH, {"plan_id": "fast-news.0a1b"}, now=NOW, starts=later)

    assert found.refusal is Refusal.OUT_OF_SCOPE


def test_a_call_that_is_not_approval_gated_takes_only_its_own_cap() -> None:
    found = verdict(programming(), Role.PROGRAMMING, "schedule___plan", {"channel": "fast-demo"}, now=NOW, gated=False)

    assert found.allowed and found.caps == (), "plan has no per-hour cap and does not count as an action"


def test_a_propose_only_agent_may_record_proposals_but_not_commit() -> None:
    agents = Envelope().agents
    envelope = Envelope(
        agents={**agents, Role.SALES: agents[Role.SALES].model_copy(update={"enabled": True})}
    ).resolved("fast")

    proposal = verdict(envelope, Role.SALES, "sales___propose", {"line": "direct-1"}, now=NOW, gated=False)
    commit = verdict(envelope, Role.SALES, "channels___start_channel", {"channel": "fast-demo"}, now=NOW)

    assert proposal.allowed and [cap.key for cap in proposal.caps] == ["CAP#sales#sales___propose"]
    assert not commit.allowed and commit.refusal is Refusal.PROPOSE_ONLY
