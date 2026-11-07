"""The envelope caps' counter against DynamoDB Local: atomic under concurrent approvals."""

import threading
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fast_channels.core.agents.envelope import Cap, window_of
from fast_channels.functions.agents.caps import take

if TYPE_CHECKING:
    from agent_tables import AgentTables

NOW = datetime(2026, 11, 7, 14, 20, tzinfo=UTC)
WINDOW = window_of(NOW)


def caps(per_channel: int = 1, overall: int = 6) -> list[Cap]:
    return [
        Cap("CAP#operations", overall, WINDOW),
        Cap("CAP#operations#channels___start_channel#fast-demo", per_channel, WINDOW),
    ]


def test_a_cap_of_one_approves_one_action_an_hour(agent_tables: AgentTables) -> None:
    first = take(agent_tables.client, agent_tables.agents, caps())
    second = take(agent_tables.client, agent_tables.agents, caps())
    next_hour = take(
        agent_tables.client,
        agent_tables.agents,
        [Cap(c.key, c.limit, WINDOW.replace(hour=WINDOW.hour + 1)) for c in caps()],
    )

    assert first is None
    assert second is not None and second.key.endswith("#fast-demo")
    assert next_hour is None
    window = WINDOW.isoformat()
    assert agent_tables.counts()[f"CAP#operations#channels___start_channel#fast-demo@{window}"] == 1
    assert agent_tables.counts()[f"CAP#operations@{window}"] == 1, "a refused action takes nothing of any cap"


def test_two_concurrent_restarts_under_a_cap_of_one_approve_exactly_one(agent_tables: AgentTables) -> None:
    for _ in range(5):  # several races, each in a window of its own
        window = WINDOW.replace(minute=0, second=_)
        racing = [Cap(c.key, c.limit, window) for c in caps()]
        barrier = threading.Barrier(2)
        results: list[Cap | None] = []
        lock = threading.Lock()

        def restart(
            racing: list[Cap] = racing,
            barrier: threading.Barrier = barrier,
            results: list[Cap | None] = results,
            lock: threading.Lock = lock,
        ) -> None:
            barrier.wait()
            found = take(agent_tables.client, agent_tables.agents, racing)
            with lock:
                results.append(found)

        threads = [threading.Thread(target=restart) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert sorted(result is None for result in results) == [False, True], results
        key = f"CAP#operations#channels___start_channel#fast-demo@{window.isoformat()}"
        assert agent_tables.counts()[key] == 1


def test_many_concurrent_actions_never_exceed_the_hourly_cap(agent_tables: AgentTables) -> None:
    overall = [Cap("CAP#programming", 3, WINDOW)]
    approved: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def act() -> None:
        barrier.wait()
        found = take(agent_tables.client, agent_tables.agents, overall)
        with lock:
            approved.append(found is None)

    threads = [threading.Thread(target=act) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert approved.count(True) == 3
    assert agent_tables.counts()[f"CAP#programming@{WINDOW.isoformat()}"] == 3


def test_a_cap_of_zero_refuses_without_writing(agent_tables: AgentTables) -> None:
    refused = take(agent_tables.client, agent_tables.agents, [Cap("CAP#sales", 0, WINDOW)])

    assert refused is not None and refused.limit == 0
    assert agent_tables.counts() == {}


def test_counters_expire_a_day_after_their_window(agent_tables: AgentTables) -> None:
    take(agent_tables.client, agent_tables.agents, caps())
    [item] = [
        i for i in agent_tables.client.scan(TableName=agent_tables.agents)["Items"] if i["pk"]["S"] == "CAP#operations"
    ]

    assert int(item["ttl"]["N"]) == int(WINDOW.timestamp()) + 3600 + 86400
