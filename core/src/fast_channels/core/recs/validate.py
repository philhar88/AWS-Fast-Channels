"""The validator every model rail passes before it is served, and the deterministic faithfulness check (P0-24).

Tiles are checked in the model's order:

1. **Policy** (a violation is counted and the tile dropped): the id must be a candidate, and the title's rating
   within the ceiling. A repeated title is dropped too.
2. **Reason** (repaired): a reason or rail title with personal data or a blocked word is a violation and is
   replaced, as is a reason that is empty or longer than :data:`REASON_MAX`. A reason that cites what the input does
   not hold for its tile (another title's name, a genre or mood the title lacks, a time, a number) is unfaithful and
   replaced by its evidence's template. Evidence codes that do not hold are dropped. Every reason served is therefore
   faithful; :attr:`Verdict.faithful` counts the model's own.
3. **Spread** (reordered, never dropped): the first :data:`TOP` tiles share the channels as evenly as the candidates
   allow (one each once there are five channels, P0-24's family rule) and hold at most :data:`SAME_GENRE_IN_TOP` of
   one genre; a pick that breaks the spread moves below the top, after the picks that keep it.
4. **Size**: rails longer than :data:`MAX_TILES` are truncated; a rail with fewer than :data:`MIN_TILES` tiles is
   invalid, and the request gets the rule-based rail instead.
"""

import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict

from fast_channels.core.recs.candidates import Candidate, Cohort, Taste
from fast_channels.core.recs.rail import Daypart, within

if TYPE_CHECKING:
    from fast_channels.core.recs.cards import Card

EVIDENCE: Final[tuple[str, ...]] = ("declared_genre", "mood_match", "new_for_you", "daypart")
MAX_TILES: Final = 8
MIN_TILES: Final = 3
TOP: Final = 5
SAME_GENRE_IN_TOP: Final = 2
TITLE_MAX: Final = 40
REASON_TARGET: Final = 90
"""The length the rubric asks for."""
REASON_MAX: Final = 120
"""The length the validator allows: models count characters loosely, so the limit sits above the target."""
PERSONAL_TITLE: Final = "Recommended for you"
COHORT_TITLE: Final = "Picked for you"
_MIN_ENTITY: Final = 4
"""Shorter titles and names are not matched in reasons: they collide with ordinary words."""

PII: Final = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"[\w.+-]+@[\w-]+\.[\w.]+",  # an email address
        r"\bsub-[\w-]+",
        r"\b[0-9a-f]{24,}\b",  # an identifier (a vid, a sub, a hash)
        r"\+?\d[\d\s().-]{8,}\d",  # a phone number
    )
)
BLOCKED: Final = re.compile(
    r"[$€£]|\b(cpms?|prices?|priced|value tiers?|tiers?|ad loads?|ads?|advert\w*|commercials?|tolerance|lambda|"
    r"sponsor\w*|ignore|override|instructions?|operator|email|vid)\b",
    re.IGNORECASE,
)
"""Commercial words and the vocabulary of prompt injection never belong in a viewer-facing reason."""
_TIME: Final = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}:\d{2}\b|\b(?:on now|right now|live now|soon|today|tomorrow|"
    r"tonight|this (?:morning|afternoon|evening|week|weekend)|in \d+\s*(?:min|mins|minutes|hours?|h))\b|"
    r"\b(?:mon|tues|wednes|thurs|fri|satur|sun)days?\b",
    re.IGNORECASE,
)
_DAYPART_WORDS: Final = {
    Daypart.MORNING: ("morning", "this morning"),
    Daypart.AFTERNOON: ("afternoon", "this afternoon"),
    Daypart.EVENING: ("evening", "this evening", "tonight"),
    Daypart.NIGHT: ("night", "late night", "tonight"),
}
_DAYPART_ENTITY: Final = re.compile(r"\b(?:morning|afternoon|evening|night)s?\b", re.IGNORECASE)
_NUMBER: Final = re.compile(r"\b\d+\b")
_WORDS: Final = re.compile(r"[a-z0-9]+")


class RailTile(BaseModel):
    """A tile of a validated model rail."""

    model_config = ConfigDict(frozen=True)

    content_id: str
    channel: str
    reason: str
    evidence: tuple[str, ...]
    replaced: bool = False
    """The model's reason was replaced by its evidence's template."""


@dataclass(slots=True)
class Verdict:
    """The validator's result for one model rail."""

    title: str
    tiles: list[RailTile] = field(default_factory=list)
    dropped: Counter[str] = field(default_factory=Counter)
    """Tiles dropped, repaired or moved, by why."""
    violations: Counter[str] = field(default_factory=Counter)
    """Policy problems in the model's output, by kind (the served rail has none)."""
    checked: int = 0
    """Model tiles whose id was a candidate: the faithfulness check's denominator."""
    faithful: int = 0
    """Of those, the tiles whose reason and evidence held unchanged."""

    @property
    def valid(self) -> bool:
        return len(self.tiles) >= MIN_TILES


def _boundary(surface: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(surface)}(?![\w-])", re.IGNORECASE)


class Entities:
    """Every title, series, channel, genre, category and mood of a catalog, the things a reason can claim."""

    def __init__(self, cards: Iterable[Card], channels: Iterable[str]) -> None:
        named: set[str] = set()
        tags: set[str] = set()
        for c in cards:
            named |= {c.title.lower(), *(s.lower() for s in [c.series] if s)}
            named |= {category.lower() for category in c.categories}
            tags |= {*c.genres, *c.moods}
        named |= {channel.lower() for channel in channels}
        self.named = sorted((s for s in named if len(s) >= _MIN_ENTITY), key=len, reverse=True)
        self.tags = sorted((s for s in tags - named if len(s) >= 3), key=len, reverse=True)

    def mentioned(self, text: str) -> tuple[list[str], str]:
        """The entities ``text`` mentions, and the text with them masked out (titles first, so that a title holding a
        genre word is read as the title)."""
        found: list[str] = []
        for surface in [*self.named, *self.tags]:
            pattern = _boundary(surface)
            if pattern.search(text):
                found.append(surface)
                text = pattern.sub(" ", text)
        return found, text


def _allowed(card: Card, channel: str, facts: Taste | Cohort) -> set[str]:
    allowed = {card.title.lower(), channel.lower(), *(s.lower() for s in [card.series] if s)}
    allowed |= {*card.genres, *card.moods, *(c.lower() for c in card.categories), *card.keywords}
    if isinstance(facts, Taste):
        allowed |= {*(g.lower() for g in facts.genres), *(m.lower() for m in facts.moods)}
    return allowed


def unsupported(reason: str, card: Card, channel: str, facts: Taste | Cohort, entities: Entities) -> list[str]:
    """The claims of ``reason`` that the input does not hold for this tile (empty: the reason is faithful). A word of
    the title's own synopsis is always supported."""
    synopsis = card.synopsis.lower()
    synopsis_words = set(_WORDS.findall(synopsis))
    found, rest = entities.mentioned(reason)
    if card.rating:
        rest = re.sub(rf"(?<![\w-]){re.escape(card.rating)}(?![\w-])", " ", rest, flags=re.IGNORECASE)
    allowed = _allowed(card, channel, facts)
    problems = [f"entity:{s}" for s in found if s not in allowed and not _boundary(s).search(synopsis)]
    daypart_words = _DAYPART_WORDS[facts.daypart] if isinstance(facts, Cohort) else ()
    problems += [
        f"time:{m.group(0)}"
        for m in _TIME.finditer(rest)
        if m.group(0).lower() not in daypart_words and m.group(0).lower() not in synopsis_words
    ]
    rest = _TIME.sub(" ", rest)
    problems += [
        f"daypart:{m.group(0)}"
        for m in _DAYPART_ENTITY.finditer(rest)
        if not any(m.group(0).lower().rstrip("s") in word for word in daypart_words)
        and m.group(0).lower().rstrip("s") not in synopsis_words
    ]
    problems += [
        f"number:{m.group(0)}"
        for m in _NUMBER.finditer(rest)
        if int(m.group(0)) != card.minutes and m.group(0) not in synopsis_words
    ]
    return problems


def holds(code: str, card: Card, facts: Taste | Cohort) -> bool:
    """Whether evidence ``code`` is true of ``card`` for ``facts``."""
    match code, facts:
        case "declared_genre", Taste():
            return bool(set(card.genres) & {g.lower() for g in facts.genres})
        case "mood_match", Taste():
            return bool(set(card.moods) & {m.lower() for m in facts.moods})
        case "new_for_you", Taste():
            return bool(facts.genres) and not set(card.genres) & {g.lower() for g in facts.genres}
        case "daypart", Cohort():
            return True  # a judgement about the cohort's daypart, which the cohort rail is built for
    return False


def template(card: Card, channel: str, facts: Taste | Cohort) -> tuple[str, tuple[str, ...]]:
    """A faithful reason for ``card`` and the evidence it rests on, used where the model's reason cannot be."""
    label = (card.categories[0] if card.categories else card.genres[0] if card.genres else "").strip()
    if isinstance(facts, Cohort):
        subject = label or card.title
        return _fit(f"{subject} for the {facts.daypart.value}"), ("daypart",)
    liked = next((g for g in (g.lower() for g in facts.genres) if g in card.genres), None)
    if liked:
        return _fit(f"Because you like {liked}"), ("declared_genre",)
    mood = next((m for m in (m.lower() for m in facts.moods) if m in card.moods), None)
    if mood:
        return _fit(f"For your taste in {mood} titles"), ("mood_match",)
    if facts.genres:
        return _fit(f"Something different: {label}" if label else f"Something different on {channel}"), ("new_for_you",)
    return _fit(f"{label} on {channel}" if label else f"On {channel}"), ()


def _fit(text: str) -> str:
    text = text[:1].upper() + text[1:]
    return text if len(text) <= REASON_TARGET else text[: REASON_TARGET - 1].rstrip() + "…"


def top_channel_limit(candidates: Iterable[Candidate]) -> int:
    """Tiles of one channel among the first :data:`TOP`: the candidates' channels share them as evenly as they can,
    so five channels or more give one each (P0-24's family rule) and a single channel may fill them."""
    return math.ceil(TOP / (len({c.channel for c in candidates}) or 1))


def _personal(text: str, forbidden: Sequence[re.Pattern[str]]) -> bool:
    return any(p.search(text) for p in PII) or any(p.search(text) for p in forbidden)


class _Validation:
    """One rail's validation: the tiles that passed policy, in the model's order, with their reasons checked."""

    def __init__(
        self,
        candidates: Sequence[Candidate],
        cards: Mapping[str, Card],
        facts: Taste | Cohort,
        forbidden: Iterable[str],
    ) -> None:
        self.cards = cards
        self.facts = facts
        self.forbidden = [_boundary(s) for s in forbidden if s]
        self.allowed = {c.content_id: c for c in candidates}
        self.per_channel_top = top_channel_limit(candidates)
        self.entities = Entities(cards.values(), {c.channel for c in candidates})
        self.kept: list[RailTile] = []
        self.verdict = Verdict(title=COHORT_TITLE if isinstance(facts, Cohort) else PERSONAL_TITLE)

    def title(self, raw: object) -> None:
        title = " ".join(str(raw or "").split())
        if BLOCKED.search(title) or _personal(title, self.forbidden):
            self.verdict.violations["title"] += 1
        elif title and len(title) <= TITLE_MAX:
            self.verdict.title = title
            return
        self.verdict.dropped["title"] += 1

    def tile(self, raw: Mapping[str, Any]) -> None:
        content_id = str(raw.get("content_id", ""))
        candidate = self.allowed.get(content_id)
        card = self.cards.get(content_id)
        if candidate is None or card is None or not within(card.rating, self.facts.rating_ceiling):
            kind = "rating_ceiling" if card and not within(card.rating, self.facts.rating_ceiling) else "not_candidate"
            self.verdict.violations[kind] += 1
            self.verdict.dropped[kind] += 1
            return
        self.verdict.checked += 1
        reason = " ".join(str(raw.get("reason", "")).split())
        codes = {str(code) for code in raw.get("evidence") or [] if str(code) in EVIDENCE}
        true_codes = tuple(code for code in EVIDENCE if code in codes and holds(code, card, self.facts))
        # A reason must rest on a true code, unless none can hold (a viewer without taste facts, say).
        grounded = bool(true_codes) or not any(holds(code, card, self.facts) for code in EVIDENCE)
        problem = self._reason_problem(reason, card, candidate.channel)
        if problem is None and grounded and len(true_codes) == len(codes):
            self.verdict.faithful += 1
        if any(t.content_id == content_id for t in self.kept):
            self.verdict.dropped["duplicate"] += 1
            return
        replace = problem is not None or not grounded
        if replace:
            self.verdict.dropped[problem or "evidence"] += 1
            reason, true_codes = template(card, candidate.channel, self.facts)
        self.kept.append(
            RailTile(
                content_id=content_id, channel=candidate.channel, reason=reason, evidence=true_codes, replaced=replace
            )
        )

    def _reason_problem(self, reason: str, card: Card, channel: str) -> str | None:
        if BLOCKED.search(reason) or _personal(reason, self.forbidden):
            self.verdict.violations["blocked_or_personal"] += 1
            return "blocked"
        if not reason or len(reason) > REASON_MAX:
            return "length"
        if unsupported(reason, card, channel, self.facts, self.entities):
            return "unfaithful"
        return None

    def arrange(self) -> None:
        """The served order: the first picks that keep the top spread, then every other pick in the model's order,
        truncated to :data:`MAX_TILES`."""
        top: list[RailTile] = []
        on_channel: Counter[str] = Counter()
        in_genre: Counter[str] = Counter()
        for tile in self.kept:
            if len(top) == TOP:
                break
            genre = (self.cards[tile.content_id].genres or ("",))[0]
            if on_channel[tile.channel] < self.per_channel_top and (not genre or in_genre[genre] < SAME_GENRE_IN_TOP):
                top.append(tile)
                on_channel[tile.channel] += 1
                in_genre[genre] += 1
        rest = [tile for tile in self.kept if tile not in top]
        arranged = top + rest
        self.verdict.dropped["moved"] += sum(a is not b for a, b in zip(arranged, self.kept, strict=True))
        self.verdict.dropped["truncated"] += max(0, len(arranged) - MAX_TILES)
        self.verdict.dropped += Counter()  # drop the zero counts
        self.verdict.tiles = arranged[:MAX_TILES]


def validate(
    output: Mapping[str, Any],
    *,
    candidates: Sequence[Candidate],
    cards: Mapping[str, Card],
    facts: Taste | Cohort,
    forbidden: Iterable[str] = (),
) -> Verdict:
    """Validate the model's rail ``output`` (the structured output) for ``facts``. ``forbidden`` are strings that must
    never be served (the viewer's names, say); the prompt never held them, so a reason that has one is a leak."""
    validation = _Validation(candidates, cards, facts, forbidden)
    validation.title(output.get("title"))
    tiles = output.get("tiles")
    for tile in tiles if isinstance(tiles, list) else []:
        if isinstance(tile, Mapping):
            validation.tile(tile)
        else:
            validation.verdict.violations["not_candidate"] += 1
    validation.arrange()
    return validation.verdict


def audit_prompt(request: str, forbidden: Iterable[str]) -> list[str]:
    """What must not be in a request block but is: ``forbidden`` strings (the viewer's id, sub, email or names) and
    commercial words. Run on every request before it is sent; a request with any is not sent."""
    hits = [s for s in forbidden if s and s.lower() in request.lower()]
    return hits + [m.group(0) for m in BLOCKED.finditer(request)]
