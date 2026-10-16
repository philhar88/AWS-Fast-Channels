"""The conformance scenarios: fill a family block (standard) and a constrained 6-hour block (hard)."""

from dataclasses import dataclass
from typing import Final

from fast_channels.agent.evals.fake_tools import CHANNEL, iso

HOUR_MS: Final = 3_600_000


@dataclass(frozen=True, slots=True)
class Scenario:
    """A scheduling task and the extra rules its score checks."""

    name: str
    hours: int
    alternate_genres: bool = False
    prime_time_title: str | None = None

    def prompt(self, start_ms: int, channel: str = CHANNEL) -> str:
        """The operator request for a block starting at ``start_ms``."""
        end_ms = start_ms + self.hours * HOUR_MS
        rules = []
        if self.alternate_genres:
            rules.append("no two consecutive programs may share a genre")
        if self.prime_time_title:
            rules.append(
                f"'{self.prime_time_title}' must be included and start at or after {iso(start_ms + 3 * HOUR_MS)}"
            )
        extra = f" Additional rules for this block: {'; '.join(rules)}." if rules else ""
        return (
            f"Fill the {self.hours}-hour block on channel {channel} from {iso(start_ms)} to {iso(end_ms)}. "
            "Follow the channel's brief, fill at least 95% of the block without running past its end, do not "
            f"repeat a title, and break roughly every 12 minutes of content.{extra} "
            "Publish the plan once it is valid."
        )


SCENARIOS: Final = {
    scenario.name: scenario
    for scenario in (
        Scenario("standard", hours=3),
        Scenario("hard", hours=6, alternate_genres=True, prime_time_title="Robo Pals: The Movie"),
    )
}
