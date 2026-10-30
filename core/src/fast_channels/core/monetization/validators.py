"""The Release Controller's validators (DESIGN §7, rules R-F1 … R-S1, plus R-A1/R-A2 for the rollout).

They run before any ``PutFunction`` or ``PutPlaybackConfiguration``, and at synth. The service enforces neither
the 1,000-character limits nor the function allow-list when a function is put (P0-20 §4.5), accepts a POST body that
is not JSON, and silently skips the ADS call when the rendered body is invalid (P0-20 §4.14): these checks are the
only guard. Every check returns all the :class:`Violation` s it finds; :func:`require_valid` raises on any.

Rules (:class:`Rule`):

- **R-F1** FunctionIds, and the documented limits: expressions ≤1,000 characters, worst-case output values ≤1,000,
  ≤20 outputs per function, ≤20 executions per hook, nesting depth ≤2, 1-10 steps, timeouts 100-2,000 ms, cache
  TTLs 1-300 s, header and URL sizes, PRE_SESSION ``player_params`` ≤1,000 characters.
- **R-F2** Only the 44 allow-listed JSONata functions.
- **R-F3** Shapes (botocore ``PutFunction``/``PutPlaybackConfiguration``), references, keys writable at the hook,
  and expressions the validators can read.
- **R-F4** Cache keys are expressions starting ``'<name>|v<N>|'`` that never read ``player_params.*`` or
  ``avail.random``, and read a session only as ``session.uuid`` (the cache is per configuration and partitioned about
  4 ways, P0-10).
- **R-F5** A PRE_ADS lookup's ``Url``, ``Headers``, ``Body`` and ``Key`` read only ``scte.segmentation_upid``,
  ``scte.event_id`` and constants. A per-session request (an auction, ``session`` or ``session_break`` scope) may also
  read ``session.uuid`` and the avail's duration, and ``session.user_agent`` in a header; never a player value.
- **R-F6** PRE_SESSION writes literals only, never ``player_params.t`` or ``manifest.*``; its root executor
  re-exports every committed key; the session controls (suppression mode and value together) are committed as
  DESIGN §5.3 sets them.
- **R-F7** Executor timeout ≤ the hook's remaining budget; each request's timeout (and their sum) < the executor's.
- **R-F8** Every ``HTTP_REQUEST`` declares a :class:`~.functions.CacheScope` and the reason for it in words
  (ADR-0034). Anything cacheable is cached: a scope other than ``none`` sets ``Cache``, whose key names exactly the
  scope's identifiers (``title``: the UPID; ``break``: the channel and the event id; ``session``: ``session.uuid``;
  ``session_break``: both; ``global``: nothing) and whose ``TtlMaximumSeconds`` stays within what one answer is valid
  for (a break scope: the shortest break; 1 s at least). ``none``, for a single-use call, sets no ``Cache``.
- **R-P1** The POST body is valid JSON when every variable renders as ``""`` and uses only ``session.uuid``,
  ``session.avail_duration_secs`` and ``scte.event_id``, each inside a JSON string; no PRE_ADS function rewrites
  the body, the URL or the variables they read.
- **R-P2** ``X-Fast-T`` is the only player-sourced header and nothing writes ``player_params.t``; ``X-Fast-Origin``
  exists only at apply time; a ticketed sibling never logs the ticket.
- **R-P3** Live and VOD maximum ≥ PRE_ADS executor timeout + ``AdsRequestTimeoutMilliseconds`` (P0-03: a wall clock
  that includes the hook).
- **R-P4** Threshold 121; ``FOLLOW_AD_SEQUENCE`` on PBS siblings, ``IGNORE_AD_SEQUENCE`` on opaque ones;
  ``AvailSuppression`` ``BEHIND_LIVE_EDGE`` ``00:00:00``; no PRE_MANIFEST function (ADR-0021, P0-07).
- **R-P5** Every grid duration has a stored request; zero, missing (300 s) and off-grid durations have none, so
  they fail safe to the slate.
- **R-P6** Sibling names ``<prefix>-<ch>-p-[a-z0-9]`` of a monetized channel.
- **R-P7** The ad-server route (M8-09): the sibling's PRE_ADS tree runs one demand-origin auction (``DemandOrigin``:
  the apply adds the secret, so it targets the auction and never sets the header itself), PRE_SESSION blanks the
  targeting player parameters, and the ADS URL passes them; a third-party ad server gets no header of ours.
- **R-C1** Channel policy ``ArnLike aws:SourceArn`` = the v3.0.0 configuration and ``<ch>-p-?``: nothing after
  ``-p-?``, no ``*`` (``<ch>*`` admitted another channel's sibling 51/72, P0-08).
- **R-S1** Every SINGLE prefetch schedule sets ``scte.event_id``, ``scte.segmentation_upid`` and
  ``session.avail_duration_ms``.
- **R-A1** The release pointer's AppConfig strategy never deploys all at once (a 0-minute deployment ignores its
  monitors), its first step is ≤10% and every step lasts longer than 2 minutes (P0-08 §3.5).
- **R-A2** Every rollout monitor treats missing data as ``notBreaching`` (``INSUFFICIENT_DATA`` rolled a
  deployment back after 5 s, P0-08).
"""

import json
import math
import re
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError

from fast_channels.core.monetization.bundle import Bundle, MonetizedChannel, Rollout, Sibling
from fast_channels.core.monetization.expressions import (
    Analysis,
    ExpressionError,
    Node,
    analyze,
    expression_body,
    max_length,
    template_variables,
)
from fast_channels.core.monetization.functions import (
    FUNCTION_ID_PATTERN,
    RELEASE_SUFFIX,
    REQUEST_PATH_HOOKS,
    SESSION_CONTROLS,
    Cache,
    CacheScope,
    Function,
    FunctionType,
    Hook,
    HttpRequestConfiguration,
    release_tag,
)
from fast_channels.core.monetization.jsonata_allowlist import ALLOWED_FUNCTIONS
from fast_channels.core.monetization.playback import (
    AD_SERVER_TARGETING,
    BODY_VARIABLES,
    HEADER_BUFFET,
    HEADER_CONFIGURATION,
    HEADER_ORIGIN,
    HEADER_PROBE,
    HEADER_RELEASE,
    HEADER_TICKET,
    PBS_AUCTION_PATH,
    PERSONALIZATION_THRESHOLD_SECONDS,
    ROLE_LETTERS,
    SEQUENCING,
    TICKET_LOG_EVENTS,
    TICKET_TEMPLATE,
    AdsRoute,
    PlaybackConfiguration,
    SiblingRole,
)
from fast_channels.core.naming import MEDIATAILOR_NAME_MAX
from fast_channels.core.specs import ADS_VARIABLES, SIBLING_PATTERN


class Rule(StrEnum):
    """Validator ids (DESIGN §7; R-A1/R-A2 cover the AppConfig rollout of §8.4)."""

    F1 = "R-F1"
    F2 = "R-F2"
    F3 = "R-F3"
    F4 = "R-F4"
    F5 = "R-F5"
    F6 = "R-F6"
    F7 = "R-F7"
    F8 = "R-F8"
    P1 = "R-P1"
    P2 = "R-P2"
    P3 = "R-P3"
    P4 = "R-P4"
    P5 = "R-P5"
    P6 = "R-P6"
    P7 = "R-P7"
    C1 = "R-C1"
    S1 = "R-S1"
    A1 = "R-A1"
    A2 = "R-A2"


class Violation(BaseModel):
    """One broken rule, where, and why."""

    model_config = ConfigDict(frozen=True)

    rule: Rule
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.rule} {self.where}: {self.message}"


class InvalidBundleError(ValueError):
    """The desired state breaks at least one rule; ``violations`` lists every one."""

    def __init__(self, violations: Iterable[Violation]) -> None:
        self.violations = tuple(violations)
        super().__init__("\n".join(str(v) for v in self.violations))


def require_valid(violations: Iterable[Violation]) -> None:
    """Raise :class:`InvalidBundleError` when ``violations`` is not empty."""
    if found := tuple(violations):
        raise InvalidBundleError(found)


# --- documented limits (UG "Functions limits" and botocore 1.43.114) -----------------------------------------------

HOOK_BUDGET_MS: Final = 2000
"""Each hook's own timeout, and the budget PRE_ADS, POST_ADS and PRE_MANIFEST share per manifest request."""
EXPRESSION_MAX: Final = 1000
OUTPUT_VALUE_MAX: Final = 1000
OUTPUTS_PER_FUNCTION_MAX: Final = 20
EXECUTIONS_PER_HOOK_MAX: Final = 20
STEPS_PER_EXECUTOR: Final = range(1, 11)
TIMEOUT_MS: Final = range(100, 2001)
CACHE_TTL_SECONDS: Final = range(1, 301)
HEADERS_MAX: Final = 50
HEADER_NAME_MAX: Final = 256
HEADER_VALUE_MAX: Final = 8192
URL_MAX: Final = 2048
BODY_MAX: Final = 64 * 1024
PLAYER_PARAMS_MAX: Final = 1000
RESTRICTED_HEADERS: Final = frozenset({"host", "transfer-encoding", "content-length", "connection"})
"""Headers MediaTailor accepts at authoring and drops at run time."""
SESSION_AWS_KEYS: Final = frozenset(
    {
        "startTime",
        "preroll",
        "overlayAvails",
        "logMode",
        "availSuppressionMode",
        "availSuppressionValue",
        "availSuppressionFillPolicy",
        "insertionMode",
        "reportingMode",
        "guidedPrefetchMode",
        "adSignalingEnabled",
        "streamId",
    }
)
"""``aws.*`` keys PRE_SESSION may write (UG, PRE_SESSION_INITIALIZATION hook); others are refused."""
WRITABLE: Final = {
    Hook.PRE_SESSION: (
        r"player_params\..+",
        r"manifest\..+",
        rf"aws\.(?:{'|'.join(sorted(SESSION_AWS_KEYS))})",
        r"temp\..+",
    ),
    Hook.PRE_ADS: (
        r"(?:player_params|session|avail|scte|asset)\..+",
        r"adsRequest\.(?:url|method|body)",
        r"adsRequest\.headers\.[^.]+",
        r"temp\..+",
    ),
    Hook.POST_ADS: (r"adsResponse\.ads", r"temp\..+"),
    Hook.PRE_MANIFEST: (r"avails\.avails", r"temp\..+"),
}
"""Output keys each hook accepts (UG, writable outputs per hook); MediaTailor validates them when a tree is mapped."""

# --- design rules --------------------------------------------------------------------------------------------------

CACHE_KEY_PREFIX: Final = re.compile(r"^[a-z0-9_]+\|v[0-9]+\|")
CACHE_KEY_FORBIDDEN: Final = ("player_params", "avail.random")
"""Never in a key: values the player sets (a viewer could pick another viewer's entry) and one that never repeats."""
SESSION_ID: Final = "session.uuid"
"""The only session value a key may read: MediaTailor generates it, unique per session."""
UPID: Final = "scte.segmentation_upid"
BREAK_IDS: Final = ("scte.event_id", "scte.segmentation_event_id")
"""A break's SCTE-35 ids. Unique per channel only, so a ``break`` key also names the channel."""
SCOPE_KEYS: Final[Mapping[CacheScope, tuple[tuple[str, ...], ...]]] = {
    CacheScope.TITLE: ((UPID,),),
    CacheScope.BREAK: (BREAK_IDS,),
    CacheScope.SESSION: ((SESSION_ID,),),
    CacheScope.SESSION_BREAK: ((SESSION_ID,), BREAK_IDS),
    CacheScope.GLOBAL: (),
}
"""R-F8: the identifiers each scope's key names, as groups; the key reads one path of every group (``scte.
segmentation_upid`` covers its sub-fields) and no other input."""
BREAK_SECONDS_MIN: Final = 30
"""The shortest break the planner airs (one 30.03 s slot), what a break-scoped answer is valid for at most."""
SCOPE_VALIDITY_SECONDS: Final[Mapping[CacheScope, int]] = {
    CacheScope.TITLE: 300,
    CacheScope.SESSION: 300,
    CacheScope.GLOBAL: 300,
}
"""R-F8: how long one answer of a scope stays right, capped by the 300 s the cache allows; the break scopes use the
shortest break (:data:`BREAK_SECONDS_MIN`, or the bundle's shortest grid duration)."""
CACHE_TTL_FLOOR_SECONDS: Final = 1
CACHE_REASON_MIN: Final = 20
"""A scope's reason is a sentence, not a token."""
CONTENT_LOOKUP_PATHS: Final = frozenset({"scte.segmentation_upid", "scte.event_id"})
SESSION_REQUEST_PATHS: Final = frozenset({SESSION_ID, "session.avail_duration_secs", "session.avail_duration_ms"})
"""What a per-session PRE_ADS request (an auction) may read besides the content ids: MediaTailor generates them."""
SESSION_REQUEST_HEADER_PATHS: Final = frozenset({"session.user_agent"})
"""Client-influenced, so only in a header (never the URL or the body, where a quote could break it; R-P1)."""
PRE_ADS_FORBIDDEN_WRITES: Final = re.compile(r"(?:session|scte|avail|asset)\..+|adsRequest\.(?:url|method|body)")
"""PRE_ADS outputs that would change the ADS URL, the body, or the variables they substitute (R-P1)."""
TICKET_KEY: Final = "player_params.t"
SUPPRESSION_KEYS: Final = ("aws.availSuppressionMode", "aws.availSuppressionValue")
AVAIL_SUPPRESSION: Final = {"Mode": "BEHIND_LIVE_EDGE", "Value": "00:00:00"}
PRESET_STRATEGY_PREFIX: Final = "AppConfig."
"""AppConfig's predefined strategies (``AppConfig.AllAtOnce`` and the rest) are never used."""
ALARM_STEP_MINUTES: Final = 2
"""An alarm needs 90-118 s from first exposure to act (P0-08 §3.5): every rollout step must last longer."""
FIRST_STEP_MAX_PERCENT: Final = 10
NOT_BREACHING: Final = "notBreaching"
PREFETCH_VARIABLES: Final = ("scte.event_id", "scte.segmentation_upid", "session.avail_duration_ms")
MISSING_DURATION_SECONDS: Final = 300
"""MediaTailor's ``avail_duration_secs`` for a zero or missing break duration (P0-20 §4.4)."""
_POLICY_ARN: Final = re.compile(r"^arn:[^:]+:mediatailor:[^:]*:[^:]*:playbackConfiguration/(?P<name>.+)$")
_PLACEHOLDER: Final = "fastvar{}x"


def _violation(rule: Rule, where: str, message: str) -> Violation:
    return Violation(rule=rule, where=where, message=message)


# --- values --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Value:
    """One value of a function document: where it is, its text, and its analysis when it is an expression."""

    where: str
    text: str
    analysis: Analysis | None = None
    error: str | None = None

    @property
    def expression(self) -> bool:
        return expression_body(self.text) is not None

    @property
    def literal(self) -> str | None:
        """The text MediaTailor commits for a constant or a string-literal expression, else None."""
        if not self.expression:
            return self.text
        return self.analysis.literal if self.analysis else None

    def bound(self, known: Mapping[str, int]) -> int | None:
        """The worst-case length of what the value evaluates to."""
        if not self.expression:
            return len(self.text)
        return max_length(self.analysis.tree, known) if self.analysis else None


def _value(where: str, text: str) -> _Value:
    body = expression_body(text)
    if body is None:
        return _Value(where, text)
    try:
        return _Value(where, text, analyze(body))
    except ExpressionError as error:
        return _Value(where, text, error=str(error))


def _request_values(function: Function) -> list[_Value]:
    """The HTTP request's evaluated values: ``Url``, ``Body``, each header and the cache key."""
    http = function.http_request_configuration
    if http is None:
        return []
    at = f"function {function.function_id}"
    values = [_value(f"{at} Url", http.url)]
    if http.body is not None:
        values.append(_value(f"{at} Body", http.body))
    values += [_value(f"{at} Headers[{name}]", text) for name, text in (http.headers or {}).items()]
    if http.cache and http.cache.key is not None:
        values.append(_value(f"{at} Cache.Key", http.cache.key))
    return values


def _values(function: Function) -> list[_Value]:
    """Every value of ``function`` that MediaTailor evaluates (output keys are not values)."""
    at = f"function {function.function_id}"
    return [
        *(_value(f"{at} Output[{key}]", text) for key, text in function.output.items()),
        *_request_values(function),
        *(
            _value(f"{at} FunctionList[{n}].RunCondition", ref.run_condition)
            for n, ref in enumerate(function.steps)
            if ref.run_condition is not None
        ),
    ]


# --- one function --------------------------------------------------------------------------------------------------


def check_function(function: Function, *, prefix: str, break_seconds: int = BREAK_SECONDS_MIN) -> list[Violation]:
    """Rules about one bundle document on its own: id, values, limits and cache (R-F1-R-F4, R-F8).

    ``break_seconds`` is the shortest break the stage airs: no break-scoped answer is cached for longer. Whether a
    ``break`` key names its sibling's channel is checked with the bundle (:func:`validate_bundle`).
    """
    at = f"function {function.function_id}"
    found: list[Violation] = []
    stem = function.function_id
    if not re.fullmatch(FUNCTION_ID_PATTERN, stem):
        found.append(_violation(Rule.F1, at, "FunctionIds use [A-Za-z0-9_] only (a hyphen fails with HTTP 400)"))
    if RELEASE_SUFFIX.search(stem):
        found.append(_violation(Rule.F1, at, "bundle ids are stems; the release suffix _rNNNN is added at apply"))
    if not stem.startswith(f"{prefix}_"):
        found.append(_violation(Rule.F1, at, f"FunctionIds start with {prefix}_"))
    if function.tags:
        found.append(_violation(Rule.F3, at, "tags are set at apply, when the id is created"))
    if len(function.output) > OUTPUTS_PER_FUNCTION_MAX:
        found.append(_violation(Rule.F1, at, f"{len(function.output)} outputs; at most {OUTPUTS_PER_FUNCTION_MAX}"))
    for value in _values(function):
        found += _check_value(value)
    if (http := function.http_request_configuration) is not None:
        found += _check_http(at, http, break_seconds=break_seconds)
    if (executor := function.sequential_executor_configuration) is not None:
        if len(executor.function_list) not in STEPS_PER_EXECUTOR:
            found.append(_violation(Rule.F1, at, f"{len(executor.function_list)} steps; an executor runs 1-10"))
        if executor.timeout_milliseconds not in TIMEOUT_MS:
            found.append(_violation(Rule.F1, at, "TimeoutMilliseconds must be 100-2,000"))
        for n, ref in enumerate(executor.function_list):
            if ref.run_condition is not None and expression_body(ref.run_condition) is None:
                found.append(_violation(Rule.F3, f"{at} FunctionList[{n}]", "RunCondition is a {% … %} expression"))
    return found


def _check_value(value: _Value) -> Iterator[Violation]:
    if value.expression and len(value.text) > EXPRESSION_MAX:
        yield _violation(Rule.F1, value.where, f"expression of {len(value.text)} characters; at most {EXPRESSION_MAX}")
    if value.error is not None:
        yield _violation(Rule.F3, value.where, f"the validators cannot read this expression: {value.error}")
    if value.analysis is not None and (blocked := sorted(value.analysis.functions - ALLOWED_FUNCTIONS)):
        names = ", ".join(f"${name}" for name in blocked)
        yield _violation(Rule.F2, value.where, f"{names} not on the allow-list (it fails at run time, P0-20)")


def _check_http(at: str, http: HttpRequestConfiguration, *, break_seconds: int) -> Iterator[Violation]:
    if http.request_timeout_milliseconds not in TIMEOUT_MS:
        yield _violation(Rule.F1, at, "RequestTimeoutMilliseconds must be 100-2,000")
    if expression_body(http.url) is None and (not re.match(r"https?://", http.url) or len(http.url) > URL_MAX):
        yield _violation(Rule.F1, f"{at} Url", f"a constant URL is http(s) and at most {URL_MAX} characters")
    if http.body is not None and expression_body(http.body) is None and len(http.body.encode()) > BODY_MAX:
        yield _violation(Rule.F1, f"{at} Body", "a request body is at most 64 KB")
    headers = http.headers or {}
    if len(headers) > HEADERS_MAX:
        yield _violation(Rule.F1, f"{at} Headers", f"{len(headers)} headers; at most {HEADERS_MAX}")
    for name, text in headers.items():
        if len(name) > HEADER_NAME_MAX or (expression_body(text) is None and len(text) > HEADER_VALUE_MAX):
            yield _violation(Rule.F1, f"{at} Headers[{name}]", "header names ≤256 and values ≤8,192 characters")
        if name.lower() in RESTRICTED_HEADERS:
            yield _violation(Rule.F1, f"{at} Headers[{name}]", "a restricted header is dropped at run time")
    yield from _check_cache_declaration(at, http)
    if (cache := http.cache) is None:
        return
    if {cache.ttl_minimum_seconds, cache.ttl_maximum_seconds} - set(CACHE_TTL_SECONDS):
        yield _violation(Rule.F1, f"{at} Cache", "TTLs are 1-300 seconds")
    if cache.ttl_maximum_seconds < cache.ttl_minimum_seconds:
        yield _violation(Rule.F1, f"{at} Cache", "TtlMaximumSeconds must be at least TtlMinimumSeconds")
    yield from _check_cache_key(f"{at} Cache.Key", cache.key)
    if http.cache_scope not in (None, CacheScope.NONE):
        yield from _check_cache_scope(f"{at} Cache", cache, http.cache_scope, break_seconds=break_seconds)


def _check_cache_declaration(at: str, http: HttpRequestConfiguration) -> Iterator[Violation]:
    """R-F8: the request declares a scope with a reason; anything but ``none`` is cached, ``none`` is not."""
    scope = http.cache_scope
    if scope is None:
        yield _violation(
            Rule.F8,
            at,
            f"declares its CacheScope, one of {[s.value for s in CacheScope]}: anything cacheable is cached, with a "
            "key that names what makes an answer unique; none only for a single-use call",
        )
        return
    if len((http.cache_reason or "").strip()) < CACHE_REASON_MIN:
        yield _violation(Rule.F8, at, f"gives the reason for CacheScope {scope} in words (a sentence, not a token)")
    if scope is CacheScope.NONE and http.cache is not None:
        yield _violation(Rule.F8, f"{at} Cache", "a CacheScope none request sets no Cache")
    if scope is not CacheScope.NONE and http.cache is None:
        yield _violation(Rule.F8, at, f"a CacheScope {scope} request sets Cache: anything cacheable is cached")


def _leftmost(node: Node) -> Node:
    """The first operand of a ``&`` chain (through one level of parentheses)."""
    while (node.kind == "binary" and node.value == "&") or (node.kind == "block" and len(node.children) == 1):
        node = node.children[0]
    return node


def _check_cache_key(where: str, key: str | None) -> Iterator[Violation]:
    value = _value(where, key or "")
    if key is None or not value.expression:
        yield _violation(Rule.F4, where, "a cached function sets Key, an expression ('<name>|v<N>|' & …)")
        return
    if value.analysis is None:
        return  # an unreadable expression is R-F3, reported with the other values
    head = _leftmost(value.analysis.tree)
    if not (head.kind == "literal" and isinstance(head.value, str) and CACHE_KEY_PREFIX.match(head.value)):
        yield _violation(Rule.F4, where, "starts with the literal '<name>|v<N>|': cache entries cannot be invalidated")
    if per_viewer := sorted(value.analysis.reads(*CACHE_KEY_FORBIDDEN)):
        yield _violation(Rule.F4, where, f"reads {per_viewer}; every session of the configuration shares the cache")
    if other := sorted(value.analysis.reads("session") - {SESSION_ID}):
        yield _violation(Rule.F4, where, f"reads {other}; a key names a session by {SESSION_ID} only")


def scope_validity(scope: CacheScope, *, break_seconds: int = BREAK_SECONDS_MIN) -> int:
    """How long one answer of ``scope`` stays right, in seconds (R-F8): at least 1, at most the cache's 300.

    Raises:
        ValueError: ``scope`` is ``none``, which is never cached.
    """
    match scope:
        case CacheScope.NONE:
            raise ValueError("a CacheScope none request is never cached")
        case CacheScope.BREAK | CacheScope.SESSION_BREAK:
            seconds = break_seconds
        case _:
            seconds = SCOPE_VALIDITY_SECONDS[scope]
    return min(max(CACHE_TTL_FLOOR_SECONDS, seconds), CACHE_TTL_SECONDS.stop - 1)


def _names(paths: Iterable[str], identifiers: Iterable[str]) -> bool:
    """Whether one of ``paths`` is one of ``identifiers`` or a field under one."""
    return any(p == i or p.startswith(f"{i}.") for p in paths for i in identifiers)


def _check_cache_scope(where: str, cache: Cache, scope: CacheScope, *, break_seconds: int) -> Iterator[Violation]:
    """R-F8 on one cached document: the identifiers its key reads, and the TTL, against its scope."""
    if cache.ttl_maximum_seconds > (validity := scope_validity(scope, break_seconds=break_seconds)):
        yield _violation(
            Rule.F8,
            where,
            f"TtlMaximumSeconds {cache.ttl_maximum_seconds} outlives a {scope} answer: at most {validity} s",
        )
    value = _value(f"{where}.Key", cache.key or "")
    if value.analysis is None:
        return  # no key (R-F4) or an unreadable one (R-F3)
    groups = SCOPE_KEYS[scope]
    paths = value.analysis.paths
    for group in groups:
        if not _names(paths, group):
            names = group[0] if len(group) == 1 else f"one of {list(group)}"
            yield _violation(Rule.F8, f"{where}.Key", f"a {scope} key names {names}")
    allowed = [identifier for group in groups for identifier in group]
    if beyond := sorted(p for p in paths if not _names([p], allowed)):
        yield _violation(
            Rule.F8,
            f"{where}.Key",
            f"reads {beyond}, which a {scope} answer does not vary by: declare the scope those values make it",
        )


def _key_segments(key: str | None) -> set[str]:
    """The ``|``-separated segments of the string literals in a cache key."""
    analysis = _value("key", key or "").analysis
    if analysis is None:
        return set()
    segments: set[str] = set()
    pending = [analysis.tree]
    while pending:
        node = pending.pop()
        if node.kind == "literal" and isinstance(node.value, str):
            segments.update(node.value.split("|"))
        pending.extend(node.children)
    return segments


def _check_break_keys(sibling: Sibling, trees: Iterable[_Tree]) -> Iterator[Violation]:
    """R-F8 for one sibling: a ``break`` key names the sibling's channel, because SCTE-35 event ids are unique per
    channel only (the cache is per configuration, and a key must not rely on that to be unique)."""
    for function in {f.function_id: f for tree in trees for f in tree.functions}.values():
        if (http := function.http_request_configuration) is None or http.cache_scope is not CacheScope.BREAK:
            continue
        if sibling.channel not in _key_segments(http.cache.key if http.cache else None):
            yield _violation(
                Rule.F8,
                f"function {function.function_id} Cache.Key",
                f"a break key of {sibling.name} names its channel as a '|{sibling.channel}|' segment",
            )


# --- one hook's tree -----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Tree:
    """A mapped function and the steps it runs, resolved from the bundle."""

    hook: Hook
    root: Function
    steps: tuple[Function, ...]

    @property
    def where(self) -> str:
        return f"{self.hook.value} tree {self.root.function_id}"

    @property
    def functions(self) -> tuple[Function, ...]:
        return (*self.steps, self.root)

    @property
    def timeout_ms(self) -> int:
        """What MediaTailor allows the tree: the executor's timeout, a lone request's, or 0 for a lone output."""
        if (executor := self.root.sequential_executor_configuration) is not None:
            return executor.timeout_milliseconds
        http = self.root.http_request_configuration
        return http.request_timeout_milliseconds if http else 0

    def committed_keys(self) -> list[str]:
        """Bindings the hook commits: an executor's ``Output`` when set, else everything its steps write."""
        if self.root.steps and not self.root.output:
            return [key for step in self.steps for key in step.output]
        return list(self.root.output)


def _resolve(bundle: Bundle, hook: Hook, stem: str, where: str) -> tuple[_Tree | None, list[Violation]]:
    root = bundle.function(stem)
    if root is None:
        return None, [_violation(Rule.F3, where, f"maps {hook.value} to {stem!r}, which the bundle does not hold")]
    steps: list[Function] = []
    problems: list[Violation] = []
    for ref in root.steps:
        step = bundle.function(ref.function_id)
        if step is None:
            problems.append(_violation(Rule.F3, f"function {stem}", f"runs {ref.function_id!r}, not in the bundle"))
        elif step.function_type is FunctionType.SEQUENTIAL_EXECUTOR:
            problems.append(
                _violation(Rule.F1, f"function {stem}", f"step {ref.function_id!r} is an executor; nesting depth ≤2")
            )
        else:
            steps.append(step)
    return _Tree(hook, root, tuple(steps)), problems


def _bounds(tree: _Tree) -> tuple[dict[str, int | None], list[Violation]]:
    """The worst-case length of every binding the tree writes (R-F1); a step reads what earlier steps wrote."""
    known: dict[str, int] = {}
    bounds: dict[str, int | None] = {}
    problems: list[Violation] = []
    for function in tree.functions:
        written: dict[str, int | None] = {}
        for key, text in function.output.items():  # one function's outputs read its inputs, not each other
            value = _value(f"function {function.function_id} Output[{key}]", text)
            written[key] = bound = value.bound(known)
            if bound is None:
                if value.error is None:
                    problems.append(_violation(Rule.F1, value.where, "cannot bound this value; cap it ($substring)"))
            elif bound > OUTPUT_VALUE_MAX:
                problems.append(_violation(Rule.F1, value.where, f"may reach {bound}; at most {OUTPUT_VALUE_MAX}"))
        known |= {key: bound for key, bound in written.items() if bound is not None}
        bounds |= written
    return bounds, problems


def _check_tree(tree: _Tree, *, routes: Collection[AdsRoute]) -> list[Violation]:
    """Rules about one hook's tree: size, bounds, writable keys, timeouts, and the hook's own rules."""
    found: list[Violation] = []
    if 1 + len(tree.steps) > EXECUTIONS_PER_HOOK_MAX:
        found.append(_violation(Rule.F1, tree.where, f"{1 + len(tree.steps)} executions; at most 20 per hook"))
    bounds, problems = _bounds(tree)
    found += problems
    writable = [re.compile(pattern) for pattern in WRITABLE[tree.hook]]
    found += [
        _violation(Rule.F3, f"function {function.function_id} Output[{key}]", f"not writable at {tree.hook.value}")
        for function in tree.functions
        for key in function.output
        if not any(pattern.fullmatch(key) for pattern in writable)
    ]
    found += _check_timeouts(tree)
    match tree.hook:
        case Hook.PRE_SESSION:
            found += _check_pre_session(tree, bounds)
        case Hook.PRE_ADS:
            found += _check_pre_ads(tree, routes)
    if tree.hook is not Hook.PRE_SESSION:
        found += [
            _violation(Rule.P2, f"function {f.function_id}", f"writes {TICKET_KEY}, the ticket")
            for f in tree.functions
            if TICKET_KEY in f.output
        ]
    return found


def _check_timeouts(tree: _Tree) -> Iterator[Violation]:
    """R-F7 inside one executor; the budget across hooks is per sibling (:func:`_check_budgets`)."""
    if not tree.root.steps:
        return
    limit = tree.timeout_ms
    requests = [(s.function_id, s.http_request_configuration) for s in tree.steps if s.http_request_configuration]
    for function_id, http in requests:
        if http.request_timeout_milliseconds >= limit:
            yield _violation(
                Rule.F7, f"function {function_id}", f"request timeout ≥ the executor's {limit} ms: its output is lost"
            )
    if len(requests) > 1 and (total := sum(h.request_timeout_milliseconds for _, h in requests)) >= limit:
        yield _violation(Rule.F7, tree.where, f"its requests may take {total} ms; the executor allows {limit} ms")


def _check_pre_session(tree: _Tree, bounds: Mapping[str, int | None]) -> Iterator[Violation]:
    """R-F6. PRE_SESSION outputs persist for the session and a remap cannot repair them (P0-08), so the tree writes
    constants only and commits the session controls exactly."""
    yield from _pre_session_writes(tree)
    yield from _player_params_size(tree, bounds)
    yield from _pre_session_commits(tree)


def _pre_session_writes(tree: _Tree) -> Iterator[Violation]:
    for function in tree.functions:
        for key, text in function.output.items():
            value = _value(f"function {function.function_id} Output[{key}]", text)
            if key == TICKET_KEY:
                yield _violation(Rule.F6, value.where, "PRE_SESSION never targets the ticket")
            if key.startswith("manifest."):
                yield _violation(Rule.F6, value.where, "PRE_SESSION never writes manifest.* (it reaches the origin)")
            if function.function_type is FunctionType.CUSTOM_OUTPUT and value.literal is None:
                yield _violation(Rule.F6, value.where, "a PRE_SESSION CUSTOM_OUTPUT writes literals only")


def _player_params_size(tree: _Tree, bounds: Mapping[str, int | None]) -> Iterator[Violation]:
    """R-F1: the committed ``player_params`` names and values total ≤1,000 characters, or MediaTailor drops them."""
    sizes = [
        (key.removeprefix("player_params."), bounds.get(key))
        for key in tree.committed_keys()
        if key.startswith("player_params.")
    ]
    if all(bound is not None for _, bound in sizes):
        total = sum(len(name) + (bound or 0) for name, bound in sizes)
        if total > PLAYER_PARAMS_MAX:
            yield _violation(Rule.F1, tree.where, f"player_params may total {total} characters; at most 1,000")


def _pre_session_commits(tree: _Tree) -> Iterator[Violation]:
    if not tree.root.steps or not tree.root.output:
        yield _violation(Rule.F6, tree.where, "PRE_SESSION maps a SEQUENTIAL_EXECUTOR whose Output commits each key")
        return
    written = {key: text for step in tree.steps for key, text in step.output.items()}
    for key in sorted(k for k in written if not k.startswith("temp.") and k not in tree.root.output):
        yield _violation(Rule.F6, tree.where, f"{key} is written but not re-exported, so it is discarded")
    committed: dict[str, str | None] = {}
    for key, text in tree.root.output.items():
        value = _value(f"function {tree.root.function_id} Output[{key}]", text)
        source = value.analysis.reexport if value.analysis else None
        if source is not None and source in written:
            committed[key] = _value(key, written[source]).literal
        elif value.literal is not None:
            committed[key] = value.literal
        else:
            yield _violation(Rule.F6, value.where, "the root commits re-exports of its steps or literals only")
    if sum(key in committed for key in SUPPRESSION_KEYS) == 1:
        yield _violation(Rule.F6, tree.where, "suppression mode and value are written together (P0-25)")
    for key, expected in SESSION_CONTROLS.items():
        if committed.get(key) != expected:
            yield _violation(Rule.F6, tree.where, f"must commit {key}={expected} (DESIGN §5.3, ADR-0021)")


def _check_pre_ads(tree: _Tree, routes: Collection[AdsRoute]) -> Iterator[Violation]:
    for function in tree.functions:
        if AdsRoute.PBS in routes:
            for key in function.output:
                if PRE_ADS_FORBIDDEN_WRITES.fullmatch(key):
                    yield _violation(
                        Rule.P1,
                        f"function {function.function_id} Output[{key}]",
                        "a PRE_ADS function never rewrites the ADS URL, the body or the variables they read",
                    )
        http = function.http_request_configuration
        per_session = http is not None and http.cache_scope in (CacheScope.SESSION, CacheScope.SESSION_BREAK)
        for value in _request_values(function):
            allowed = CONTENT_LOOKUP_PATHS
            if per_session:
                if value.where.endswith("Cache.Key"):
                    continue  # a per-session key names the session: R-F8 holds it to its scope
                allowed = allowed | SESSION_REQUEST_PATHS
                if "Headers[" in value.where:
                    allowed = allowed | SESSION_REQUEST_HEADER_PATHS
            if value.analysis and (extra := sorted(value.analysis.paths - allowed)):
                rule = (
                    "a per-session PRE_ADS request reads the SCTE-35 ids, the session id and the avail's duration"
                    if per_session
                    else "a PRE_ADS lookup reads only the SCTE-35 ids and constants"
                )
                yield _violation(Rule.F5, value.where, f"reads {extra}; {rule}")


# --- the ADS request -----------------------------------------------------------------------------------------------


def check_body_template(body: str | None, *, where: str = "body") -> list[Violation]:
    """R-P1: MediaTailor inserts values without JSON escaping and sends nothing when the result is not JSON."""
    if body is None:
        return [_violation(Rule.P1, where, "a PBS sibling POSTs a body")]
    found: list[Violation] = []
    names = template_variables(body)
    if unknown := sorted(set(names) - BODY_VARIABLES):
        found.append(
            _violation(Rule.P1, where, f"uses {unknown}; the body takes only {sorted(BODY_VARIABLES)} and constants")
        )
    empty = _render_template(body, lambda _: "")
    try:
        json.loads(empty)
    except json.JSONDecodeError:
        found.append(
            _violation(Rule.P1, where, "not valid JSON when a variable renders empty (MediaTailor skips the call)")
        )
        return found
    placeholders = {name: _PLACEHOLDER.format(n) for n, name in enumerate(dict.fromkeys(names))}
    try:
        strings = list(_strings(json.loads(_render_template(body, placeholders.__getitem__))))
    except json.JSONDecodeError:
        strings = []
    for name, token in placeholders.items():
        if not any(token in text for text in strings):
            found.append(_violation(Rule.P1, where, f"{{{{{name}}}}} must sit inside a JSON string"))
    return found


def _render_template(template: str, value: Any) -> str:
    return re.sub(r"\{\{([^{}]*)\}\}", lambda m: value(m.group(1).strip()), template)


def _strings(node: Any) -> Iterator[str]:
    """Every string value (not key) of a JSON document."""
    match node:
        case str():
            yield node
        case dict():
            for item in node.values():
                yield from _strings(item)
        case list():
            for item in node:
                yield from _strings(item)


def _known_variable(name: str) -> bool:
    families = [v.removesuffix("*") for v in ADS_VARIABLES if v.endswith(".*")]
    return name in ADS_VARIABLES or any(name.startswith(f) and len(name) > len(f) for f in families)


def check_headers(sibling: Sibling) -> list[Violation]:
    """R-P2 on the bundle's request: player values only in ``X-Fast-T`` (never in the URL), no origin secret yet, the
    probe flag on the probe only, and no ticket in the logs of a sibling that forwards one."""
    where = f"sibling {sibling.name} Headers"
    headers = sibling.playback.headers
    found: list[Violation] = []
    url_params = set(re.findall(r"\[(player_params\.[^\]]*)\]", sibling.playback.ad_decision_server_url))
    if sibling.route is AdsRoute.AD_SERVER:
        url_params -= {f"player_params.{key}" for key in AD_SERVER_TARGETING}  # the auction's, not the player's
    if url_params:
        found.append(
            _violation(
                Rule.P2, f"sibling {sibling.name} AdDecisionServerUrl", f"carries {sorted(url_params)}; never the URL"
            )
        )
    if not sibling.playback.sends_origin:
        if headers:
            found.append(_violation(Rule.P2, where, "a third-party ad server gets no header of ours"))
        return found
    for name, text in headers.items():
        variables = template_variables(text)
        if unknown := [v for v in variables if not _known_variable(v)]:
            found.append(_violation(Rule.P2, f"{where}[{name}]", f"unknown MediaTailor variables {unknown}"))
        if any(v.startswith("player_params.") for v in variables) and (name, text) != (HEADER_TICKET, TICKET_TEMPLATE):
            found.append(
                _violation(Rule.P2, f"{where}[{name}]", f"only {HEADER_TICKET}: {TICKET_TEMPLATE} is player-sourced")
            )
        if name.lower() in {HEADER_ORIGIN.lower(), HEADER_RELEASE.lower()}:
            found.append(_violation(Rule.P2, f"{where}[{name}]", "set only at apply (Secrets Manager, the release)"))
    if headers.get(HEADER_CONFIGURATION) != sibling.name:
        found.append(_violation(Rule.P2, where, f"{HEADER_CONFIGURATION} names the sibling, {sibling.name}"))
    if (HEADER_PROBE in headers) != (sibling.role is SiblingRole.PROBE):
        found.append(_violation(Rule.P2, where, f"{HEADER_PROBE} is set on the probe sibling, and only there"))
    if HEADER_TICKET in headers and (logged := sorted(sibling.playback.opt_in_events & TICKET_LOG_EVENTS)):
        found.append(_violation(Rule.P2, f"sibling {sibling.name}", f"forwards the ticket and logs it in {logged}"))
    return found


def check_stored_requests(
    body: str | None, *, stored_requests: Collection[str], grid_seconds: Collection[int], where: str = "body"
) -> list[Violation]:
    """R-P5: the stored request the body names exists for every grid duration and for nothing else, so a zero,
    missing (300 s) or off-grid duration asks for one that does not exist, PBS answers 400 and the slate plays."""
    match = re.search(r'"id"\s*:\s*"([A-Za-z0-9_-]*)\{\{\s*session\.avail_duration_secs\s*\}\}s"', body or "")
    if match is None:
        return [_violation(Rule.P5, where, "the body names its stored request by session.avail_duration_secs")]
    prefix = match.group(1)
    served = {int(m.group(1)) for r in stored_requests if (m := re.fullmatch(rf"{re.escape(prefix)}(\d+)s", r))}
    found = [
        _violation(Rule.P5, where, f"no stored request {prefix}{seconds}s for the {seconds} s breaks")
        for seconds in sorted(set(grid_seconds) - served)
    ]
    found += [
        _violation(Rule.P5, where, f"{prefix}{seconds}s exists: a {seconds} s ask must fail safe to the slate")
        for seconds in sorted(served - set(grid_seconds))
    ]
    if MISSING_DURATION_SECONDS in grid_seconds or 0 in grid_seconds:
        found.append(_violation(Rule.P5, where, "0 s and 300 s (a missing duration) are never grid durations"))
    return found


def _check_playback(sibling: Sibling, bundle: Bundle, trees: Mapping[Hook, _Tree]) -> Iterator[Violation]:
    """R-P1, R-P3-R-P5 and R-F7 across hooks for one sibling."""
    playback, where = sibling.playback, f"sibling {sibling.name}"
    if sibling.route is AdsRoute.PBS:
        yield from check_body_template(playback.body, where=f"{where} Body")
        yield from check_stored_requests(
            playback.body,
            stored_requests=bundle.stored_requests,
            grid_seconds=bundle.grid_seconds,
            where=f"{where} Body",
        )
    yield from check_headers(sibling)
    yield from _check_ad_server(sibling, bundle, trees)
    yield from check_live_maximum(playback, pre_ads_ms=trees[Hook.PRE_ADS].timeout_ms if Hook.PRE_ADS in trees else 0)
    yield from check_ad_behaviour(sibling)
    yield from _check_budgets(where, trees)


_AUCTION_BODY: Final = re.compile(r"'([A-Za-z0-9_-]*)' & \$string\(session\.avail_duration_secs\) & 's'")
"""How an auction's JSONata body names its stored request (``core.ad_server.auction_body``)."""


def _origin_requests(trees: Iterable[_Tree]) -> list[Function]:
    return [
        f
        for tree in trees
        for f in tree.functions
        if f.http_request_configuration is not None and f.http_request_configuration.demand_origin
    ]


def check_demand_origin(function: Function) -> list[Violation]:
    """R-P7 on a demand-origin request: a POST to the auction that leaves the origin secret and the release to the
    apply, so the secret goes nowhere else and the bundle never holds it."""
    http, where = function.http_request_configuration, f"function {function.function_id}"
    if http is None or not http.demand_origin:
        return []
    found: list[Violation] = []
    head = _value(f"{where} Url", http.url)
    url = head.text if not head.expression else (_leftmost(head.analysis.tree).value if head.analysis else "")
    if not (isinstance(url, str) and url.startswith("https://") and f"{PBS_AUCTION_PATH}?" in url):
        found.append(
            _violation(Rule.P7, f"{where} Url", f"a demand-origin request POSTs to https://<origin>{PBS_AUCTION_PATH}?")
        )
    if http.method_type != "POST":
        found.append(_violation(Rule.P7, where, "a demand-origin request is a POST"))
    if {name.lower() for name in http.headers or {}} & {HEADER_ORIGIN.lower(), HEADER_RELEASE.lower()}:
        found.append(_violation(Rule.P7, f"{where} Headers", "the apply adds the origin secret and the release"))
    return found


def _check_ad_server(sibling: Sibling, bundle: Bundle, trees: Mapping[Hook, _Tree]) -> Iterator[Violation]:
    """R-P7 and R-P5 for one sibling: an ad-server sibling runs one auction, whose stored request exists for every
    grid duration, and blanks the targeting at session start; no other sibling runs one."""
    where = f"sibling {sibling.name}"
    auctions = _origin_requests([trees[Hook.PRE_ADS]] if Hook.PRE_ADS in trees else [])
    if _origin_requests([tree for hook, tree in trees.items() if hook is not Hook.PRE_ADS]):
        yield _violation(Rule.P7, where, "a demand-origin request runs only at PRE_ADS")
    if sibling.route is not AdsRoute.AD_SERVER:
        if auctions:
            yield _violation(Rule.P7, where, "only an ad-server sibling runs a PRE_ADS auction")
        return
    if len(auctions) != 1:
        yield _violation(Rule.P7, where, f"an ad-server sibling's PRE_ADS tree runs one auction, not {len(auctions)}")
    for auction in auctions:
        yield from check_demand_origin(auction)
        body = (auction.http_request_configuration.body if auction.http_request_configuration else None) or ""
        at = f"function {auction.function_id} Body"
        if (match := _AUCTION_BODY.search(body)) is None:
            yield _violation(Rule.P5, at, "names its stored request by the break's length")
            continue
        template = f'{{"id":"{match.group(1)}{{{{session.avail_duration_secs}}}}s"}}'
        yield from check_stored_requests(
            template, stored_requests=bundle.stored_requests, grid_seconds=bundle.grid_seconds, where=at
        )
    session = trees.get(Hook.PRE_SESSION)
    committed = set(session.root.output) if session else set()
    if missing := sorted({f"player_params.{key}" for key in AD_SERVER_TARGETING} - committed):
        yield _violation(Rule.P7, where, f"PRE_SESSION commits {missing} empty, so a player cannot set them")


def check_live_maximum(playback: PlaybackConfiguration, *, pre_ads_ms: int) -> list[Violation]:
    """R-P3: the Live (and VOD) maximum is a wall clock that starts before the PRE_ADS hook, and what it cuts is the
    ADS call, or the call is never sent (P0-03); it covers the PRE_ADS tree's timeout plus the ADS timeout."""
    timeouts = playback.ads_personalization_timeouts
    ads = timeouts.ads_request_timeout_milliseconds if timeouts else None
    maxima = {
        "LiveMaximumAdsPersonalizationTimeMilliseconds": timeouts
        and timeouts.live_maximum_ads_personalization_time_milliseconds,
        "VodMaximumAdsPersonalizationTimeMilliseconds": timeouts
        and timeouts.vod_maximum_ads_personalization_time_milliseconds,
    }
    return [
        _violation(
            Rule.P3,
            f"sibling {playback.name}",
            f"{label} must be at least PRE_ADS {pre_ads_ms} ms + AdsRequestTimeout {ads} ms",
        )
        for label, maximum in maxima.items()
        if ads is None or not maximum or maximum < pre_ads_ms + ads
    ]


def check_ad_behaviour(sibling: Sibling) -> list[Violation]:
    """R-P4 (ADR-0021): threshold 121, the sequencing of the route with ad buffet on, the suppression constant, and
    no PRE_MANIFEST injection (a transcode path that drops first injections and is billed, P0-07)."""
    playback, where = sibling.playback, f"sibling {sibling.name}"
    found: list[Violation] = []
    if playback.personalization_threshold_seconds != PERSONALIZATION_THRESHOLD_SECONDS:
        found.append(_violation(Rule.P4, where, "PersonalizationThresholdSeconds is 121 (slate plays unwanted slots)"))
    if playback.ad_sequencing_mode != SEQUENCING[sibling.route]:
        found.append(_violation(Rule.P4, where, f"a {sibling.route} sibling uses {SEQUENCING[sibling.route]}"))
    if SEQUENCING[sibling.route] == "FOLLOW_AD_SEQUENCE" and HEADER_BUFFET in playback.headers:
        found.append(_violation(Rule.P4, where, f"{HEADER_BUFFET} turns off the standalone ads FOLLOW relies on"))
    suppression = playback.avail_suppression.api() if playback.avail_suppression else {}
    if suppression != AVAIL_SUPPRESSION:
        found.append(_violation(Rule.P4, where, "AvailSuppression is BEHIND_LIVE_EDGE 00:00:00 (defence in depth)"))
    if Hook.PRE_MANIFEST in (playback.function_mapping or {}):
        found.append(_violation(Rule.P4, where, "no PRE_MANIFEST function: injection is a transcode path (P0-07)"))
    return found


def _check_budgets(where: str, trees: Mapping[Hook, _Tree]) -> Iterator[Violation]:
    if Hook.PRE_SESSION in trees and trees[Hook.PRE_SESSION].timeout_ms > HOOK_BUDGET_MS:
        yield _violation(Rule.F7, where, f"the PRE_SESSION tree outlasts its {HOOK_BUDGET_MS} ms hook")
    remaining = HOOK_BUDGET_MS
    for hook in (h for h in REQUEST_PATH_HOOKS if h in trees):
        if trees[hook].timeout_ms > remaining:
            yield _violation(
                Rule.F7, where, f"{hook.value} may need {trees[hook].timeout_ms} ms; {remaining} ms remain"
            )
        remaining -= trees[hook].timeout_ms


def check_sibling_name(sibling: Sibling, *, prefix: str, channels: Collection[str]) -> list[Violation]:
    """R-P6: ``<prefix>-<ch>-p-[a-z0-9]`` of a monetized channel, the letter of its role, at most 64 characters."""
    where = f"sibling {sibling.name}"
    found: list[Violation] = []
    if sibling.channel not in channels or not sibling.channel.startswith(f"{prefix}-"):
        found.append(_violation(Rule.P6, where, f"{sibling.channel!r} is not a monetized channel of {prefix!r}"))
    if (
        not re.fullmatch(rf"{re.escape(sibling.channel)}-p-[a-z0-9]", sibling.name)
        or sibling.name[-1] != sibling.letter
    ):
        found.append(_violation(Rule.P6, where, f"named {sibling.channel}-p-<letter>, letter {sibling.letter!r}"))
    if sibling.letter not in ROLE_LETTERS[sibling.role]:
        found.append(_violation(Rule.P6, where, f"a {sibling.role} sibling is one of {ROLE_LETTERS[sibling.role]}"))
    if len(sibling.name) > MEDIATAILOR_NAME_MAX or sibling.playback.name != sibling.name:
        found.append(_violation(Rule.P6, where, "the configuration carries the sibling's name, ≤64 characters"))
    return found


# --- channel policy, prefetch, rollout ----------------------------------------------------------------------------


def check_channel_policy(policy: Mapping[str, Any], *, channel: str) -> list[Violation]:
    """R-C1 on a channel policy document of a monetized channel.

    Every MediaTailor ``GetManifest`` statement admits exactly the v3.0.0 configuration (named like the channel)
    and the static sibling pattern ``<channel>-p-?``; ``?`` is exactly one character, so no other channel's sibling
    and no longer name matches. The pattern is set when the channel is created and never changed by a release.
    """
    where = f"channel {channel} policy"
    statements = [
        s
        for s in policy.get("Statement", [])
        if isinstance(s, Mapping) and (s.get("Principal") or {}).get("Service") == "mediatailor.amazonaws.com"
    ]
    if not statements:
        return [_violation(Rule.C1, where, "no statement lets MediaTailor read the channel")]
    found: list[Violation] = []
    expected = {channel, f"{channel}{SIBLING_PATTERN}"}
    for n, statement in enumerate(statements):
        at = f"{where} Statement[{n}]"
        condition = statement.get("Condition") or {}
        if "aws:SourceAccount" not in (condition.get("StringEquals") or {}):
            found.append(_violation(Rule.C1, at, "conditions on aws:SourceAccount"))
        arns = (condition.get("ArnLike") or {}).get("aws:SourceArn")
        values = [arns] if isinstance(arns, str) else list(arns or [])
        names = [m.group("name") if (m := _POLICY_ARN.match(str(v))) else None for v in values]
        if any("*" in str(v) for v in values):
            found.append(_violation(Rule.C1, at, "no '*' in aws:SourceArn (<ch>* admits other channels' siblings)"))
        if None in names or sorted(n for n in names if n) != sorted(expected) or len(names) != len(expected):
            found.append(
                _violation(
                    Rule.C1, at, f"ArnLike aws:SourceArn is exactly the playback configurations {sorted(expected)}"
                )
            )
    return found


def _check_channel(channel: MonetizedChannel, siblings: Iterable[Sibling]) -> Iterator[Violation]:
    where = f"channel {channel.name}"
    if channel.policy_source_arns != (channel.name, f"{channel.name}{SIBLING_PATTERN}"):
        yield _violation(Rule.C1, where, "the policy admits the channel and <channel>-p-? only")
    general = sorted(s.letter for s in siblings if s.channel == channel.name and s.role is SiblingRole.GENERAL)
    if general != sorted(ROLE_LETTERS[SiblingRole.GENERAL]):
        yield _violation(Rule.P6, where, f"has the sibling pair a/b, which swap stable and idle; found {general}")


def check_prefetch_schedule(request: Mapping[str, Any]) -> list[Violation]:
    """R-S1 on ``CreatePrefetchSchedule`` arguments: a SINGLE schedule carries the break's ids and duration, or the
    retrieved request has no UPID (P0-02) and the PDS cannot match it to the schedule of record."""
    if request.get("ScheduleType", "SINGLE") != "SINGLE":
        return []
    variables = (request.get("Retrieval") or {}).get("DynamicVariables") or {}
    missing = [name for name in PREFETCH_VARIABLES if not variables.get(name)]
    where = f"prefetch schedule {request.get('Name', '?')}"
    return [_violation(Rule.S1, where, f"DynamicVariables must set {missing}")] if missing else []


def _steps(rollout: Rollout) -> int:
    if rollout.growth_type == "LINEAR":
        return math.ceil(100 / rollout.growth_factor)
    return math.ceil(math.log2(100 / rollout.growth_factor)) + 1


def check_rollout(rollout: Rollout) -> list[Violation]:
    """R-A1 on the release pointer's AppConfig strategy."""
    where = f"rollout {rollout.name}"
    found: list[Violation] = []
    if rollout.name.startswith(PRESET_STRATEGY_PREFIX) or rollout.deployment_duration_in_minutes == 0:
        found.append(_violation(Rule.A1, where, "never all at once: a 0-minute deployment ignores its monitors"))
    if rollout.growth_factor > FIRST_STEP_MAX_PERCENT:
        found.append(_violation(Rule.A1, where, f"the first step exposes {rollout.growth_factor}%; at most 10%"))
    if rollout.deployment_duration_in_minutes / _steps(rollout) <= ALARM_STEP_MINUTES:
        found.append(_violation(Rule.A1, where, "every step lasts longer than 2 minutes (an alarm needs 90-118 s)"))
    if rollout.final_bake_time_in_minutes <= ALARM_STEP_MINUTES:
        found.append(_violation(Rule.A1, where, "the final bake lasts longer than 2 minutes"))
    return found


def check_monitors(treat_missing_data: Mapping[str, str | None]) -> list[Violation]:
    """R-A2: each rollout monitor (alarm name → ``TreatMissingData``) is ``notBreaching``; a new sibling has no
    traffic yet, and an alarm in ``INSUFFICIENT_DATA`` rolls the deployment back."""
    return [
        _violation(Rule.A2, f"alarm {name}", f"TreatMissingData is {NOT_BREACHING}, not {value or 'missing'}")
        for name, value in treat_missing_data.items()
        if value != NOT_BREACHING
    ]


# --- the bundle ----------------------------------------------------------------------------------------------------


def validate_bundle(bundle: Bundle) -> list[Violation]:
    """Every rule over a bundle: each function, each mapped tree once, each sibling, each channel, the rollout."""
    found: list[Violation] = []
    break_seconds = min(bundle.grid_seconds, default=BREAK_SECONDS_MIN)
    for function in bundle.functions:
        found += check_function(function, prefix=bundle.prefix, break_seconds=break_seconds)
    trees: dict[tuple[Hook, str], _Tree] = {}
    routes: dict[tuple[Hook, str], set[AdsRoute]] = {}
    mapped: set[str] = set()
    channels = [c.name for c in bundle.channels]
    for sibling in bundle.siblings:
        found += check_sibling_name(sibling, prefix=bundle.prefix, channels=channels)
        sibling_trees: dict[Hook, _Tree] = {}
        for hook, stem in (sibling.playback.function_mapping or {}).items():
            key = (hook, stem)
            if key not in trees:
                tree, problems = _resolve(bundle, hook, stem, f"sibling {sibling.name}")
                found += problems
                if tree is None:
                    continue
                trees[key] = tree
            sibling_trees[hook] = trees[key]
            routes.setdefault(key, set()).add(sibling.route)
            mapped |= {f.function_id for f in trees[key].functions}
        found += _check_playback(sibling, bundle, sibling_trees)
        found += _check_break_keys(sibling, sibling_trees.values())
    for key, tree in trees.items():
        found += _check_tree(tree, routes=routes[key])
    found += [
        _violation(Rule.F3, f"function {f.function_id}", "no sibling maps it; the bundle holds only mapped trees")
        for f in bundle.functions
        if f.function_id not in mapped
    ]
    for channel in bundle.channels:
        found += _check_channel(channel, bundle.siblings)
    found += check_rollout(bundle.rollout)
    return found


def load_bundle(document: Mapping[str, Any]) -> tuple[Bundle | None, list[Violation]]:
    """Parse a stored bundle document (``{"digest", "bundle"}``) and validate it.

    A shape that does not parse (an unknown member, a missing configuration, ...) is R-F3; so is a digest that does
    not match the content.
    """
    try:
        bundle = Bundle.model_validate(document.get("bundle"))
    except ValidationError as error:
        problems = [
            _violation(Rule.F3, "bundle " + ".".join(str(p) for p in e["loc"]), e["msg"]) for e in error.errors()
        ]
        return None, problems
    found = validate_bundle(bundle)
    if document.get("digest") != bundle.digest:
        found.append(_violation(Rule.F3, "bundle", "the stored digest does not match the content"))
    return bundle, found


# --- at apply ------------------------------------------------------------------------------------------------------


def check_rendered(put: Mapping[str, Any], *, release: int, secrets: Sequence[SecretStr]) -> list[Violation]:
    """Checks of a rendered ``PutPlaybackConfiguration`` request just before it is sent (R-P2, R-F1).

    ``secrets`` are the origin values the rule group accepts; the request carries one of them in ``X-Fast-Origin``
    and nowhere else, ``X-Fast-Fv`` names ``release``, and every mapped FunctionId is of that release.
    """
    where = f"rendered {put.get('Name', '?')}"
    found: list[Violation] = []
    headers = ((put.get("AdDecisionServerConfiguration") or {}).get("HttpRequest") or {}).get("Headers") or {}
    values = {s.get_secret_value() for s in secrets}
    if headers.get(HEADER_ORIGIN) not in values:
        found.append(_violation(Rule.P2, where, f"{HEADER_ORIGIN} carries a current origin secret"))
    rest = json.dumps({**put, "AdDecisionServerConfiguration": None}) + json.dumps(
        {k: v for k, v in headers.items() if k != HEADER_ORIGIN}
    )
    body = ((put.get("AdDecisionServerConfiguration") or {}).get("HttpRequest") or {}).get("Body") or ""
    if any(secret and (secret in rest or secret in body) for secret in values):
        found.append(_violation(Rule.P2, where, f"the origin secret appears outside {HEADER_ORIGIN}"))
    if headers.get(HEADER_RELEASE) != release_tag(release):
        found.append(_violation(Rule.P2, where, f"{HEADER_RELEASE} is {release_tag(release)}"))
    suffix = f"_{release_tag(release)}"
    found += [
        _violation(Rule.F1, where, f"{hook} maps {function_id!r}, not a FunctionId of {release_tag(release)}")
        for hook, function_id in (put.get("FunctionMapping") or {}).items()
        if not (function_id.endswith(suffix) and re.fullmatch(FUNCTION_ID_PATTERN, function_id))
    ]
    return found


def check_secret_free(text: str, *, secrets: Iterable[SecretStr], where: str) -> list[Violation]:
    """R-P2: the origin secret never appears in synth output, the bundle or a log line (``text``)."""
    if any((value := s.get_secret_value()) and value in text for s in secrets):
        return [_violation(Rule.P2, where, "contains the origin secret")]
    return []
