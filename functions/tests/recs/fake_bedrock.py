"""A stand-in Bedrock Runtime client for the recommender's tests: it answers ``converse`` like a well-behaved model,
with the request's candidates in score order and a faithful reason each, or with what a test scripts."""

import json
import re
from collections.abc import Callable
from typing import Any

from fast_channels.core.recs.candidates import Cohort, Taste
from fast_channels.core.recs.rail import Daypart
from fast_channels.core.recs.validate import MAX_TILES, template
from fast_channels.functions.recs.evals import fixture

USAGE = {"inputTokens": 1_200, "outputTokens": 600, "cacheReadInputTokens": 9_000, "cacheWriteInputTokens": 0}
_BLOCK = re.compile(r"```json\n(.*)\n```", re.DOTALL)


def request_of(parameters: dict[str, Any]) -> dict[str, Any]:
    """The request block's JSON."""
    text = parameters["messages"][0]["content"][0]["text"]
    match = _BLOCK.search(text)
    assert match, text
    loaded: dict[str, Any] = json.loads(match.group(1))
    return loaded


def obedient(parameters: dict[str, Any]) -> dict[str, Any]:
    """A rail of the candidates in score order, within the request's channel limits, with template reasons."""
    request = request_of(parameters)
    cards = fixture.cards()
    facts: Taste | Cohort
    if "cohort" in request:
        cohort = request["cohort"]
        facts = Cohort(device=cohort["device"], language=cohort["language"], daypart=Daypart(cohort["daypart"]))
    else:
        facts = Taste(genres=tuple(request["taste"]["genres"]), moods=tuple(request["taste"]["moods"]))
    tiles: list[dict[str, Any]] = []
    per_channel: dict[str, int] = {}
    for candidate in request["candidates"]:
        channel = candidate["channel"]
        limit = request["max_per_channel_top5"] if len(tiles) < 5 else MAX_TILES
        if per_channel.get(channel, 0) >= limit:
            continue
        reason, evidence = template(cards[candidate["id"]], channel, facts)
        tiles.append({"content_id": candidate["id"], "reason": reason, "evidence": list(evidence)})
        per_channel[channel] = per_channel.get(channel, 0) + 1
        if len(tiles) == MAX_TILES:
            break
    return {"title": "Picked for you", "tiles": tiles}


class FakeBedrock:
    """``converse`` answers with ``answer(parameters)`` (a rail document), or raises what ``answer`` raises."""

    def __init__(self, answer: Callable[[dict[str, Any]], Any] = obedient, *, stop_reason: str = "end_turn") -> None:
        self.answer = answer
        self.stop_reason = stop_reason
        self.calls: list[dict[str, Any]] = []

    def converse(self, **parameters: Any) -> dict[str, Any]:
        self.calls.append(parameters)
        document = self.answer(parameters)
        text = document if isinstance(document, str) else json.dumps(document)
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "stopReason": self.stop_reason,
            "usage": {**USAGE, "totalTokens": 10_800},
            "metrics": {"latencyMs": 4_600},
            "ResponseMetadata": {"RequestId": f"req-{len(self.calls)}"},
        }
