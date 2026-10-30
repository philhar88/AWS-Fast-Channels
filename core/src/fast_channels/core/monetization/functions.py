"""MediaTailor Monetization Functions as documents: the ``PutFunction`` shape, FunctionIds, and the session controls.

The models mirror botocore's ``PutFunctionRequest`` (field names in PascalCase on the wire, ``extra`` forbidden),
for the three function types the bundle uses: ``CUSTOM_OUTPUT``, ``HTTP_REQUEST`` (with ``Cache``, which only the API
accepts) and ``SEQUENTIAL_EXECUTOR``. They check the shape only; the service limits and the design rules are the
validators' job (:mod:`fast_channels.core.monetization.validators`), which report every problem at once. An
``HTTP_REQUEST`` also declares its :class:`CacheScope` and the reason for it, which the bundle keeps and requests leave
out.

**FunctionIds are immutable.** An id is ``<p>_[<sibling>_]<name>_rNNNN``: ``<p>`` is the deployment prefix with
``-`` replaced by ``_`` (hyphens fail with HTTP 400), the sibling scope appears only in functions that embed a
playback-configuration constant, and ``rNNNN`` is the release that created it. A mapped id is never updated: in-place
updates leave stragglers for up to 11.5 s (P0-08 §3.4) and ``PutFunction`` with ``Tags`` on an existing id fails
(W-2 1.16). The bundle therefore stores *stems* (the id without the release) and :meth:`Function.released` renders the
ids of one release.
"""

import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)
from pydantic.alias_generators import to_pascal

RUNTIME: Final = "JSONATA"
FUNCTION_ID_PATTERN: Final = r"^[A-Za-z0-9_]+$"
RELEASE_SUFFIX: Final = re.compile(r"_r[0-9]{4}$")
MAX_RELEASE: Final = 9999


class Hook(StrEnum):
    """Where MediaTailor runs a mapped function (``FunctionMapping`` keys)."""

    PRE_SESSION = "PRE_SESSION_INITIALIZATION"
    PRE_ADS = "PRE_ADS_REQUEST"
    POST_ADS = "POST_ADS_RESPONSE"
    PRE_MANIFEST = "PRE_MANIFEST_INSERTION"

    @property
    def abbreviation(self) -> str:
        """The hook's short name in FunctionIds: ``ps``, ``pa``, ``po``, ``pm``."""
        return _ABBREVIATIONS[self]


_ABBREVIATIONS: Final = {Hook.PRE_SESSION: "ps", Hook.PRE_ADS: "pa", Hook.POST_ADS: "po", Hook.PRE_MANIFEST: "pm"}
REQUEST_PATH_HOOKS: Final = (Hook.PRE_ADS, Hook.POST_ADS, Hook.PRE_MANIFEST)
"""Hooks that share one 2,000 ms budget per manifest request, in the order they run (PRE_SESSION has its own)."""


class FunctionType(StrEnum):
    """The function types the bundle builds (botocore also has ``AWS_SERVICE_REQUEST``, ``CONCURRENT_EXECUTOR`` and
    ``VAST_REQUEST``, which no module uses yet)."""

    CUSTOM_OUTPUT = "CUSTOM_OUTPUT"
    HTTP_REQUEST = "HTTP_REQUEST"
    SEQUENTIAL_EXECUTOR = "SEQUENTIAL_EXECUTOR"


API_CONTEXT: Final = "fast-api"
"""Serialization context flag of :meth:`ApiShape.api`: members that only the bundle holds are left out."""


class ApiShape(BaseModel):
    """An AWS API structure: snake_case in Python, PascalCase on the wire, unknown members refused."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        alias_generator=to_pascal,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )

    def api(self) -> dict[str, Any]:
        """The structure as API keyword arguments (unset optional members and bundle-only members omitted)."""
        return self.model_dump(mode="json", exclude_none=True, context={API_CONTEXT: True})


class CacheScope(StrEnum):
    """What one answer of an ``HTTP_REQUEST`` is valid for, and so what its cache ``Key`` names (validator R-F8,
    ADR-0033). Anything cacheable is cached; ``none`` is for a call that must happen every time."""

    TITLE = "title"
    """One answer per title for every session and break: the key names the break's UPID (the content id)."""
    BREAK = "break"
    """One answer per break of a channel: the key names the channel and the break's SCTE-35 event id."""
    SESSION = "session"
    """One answer per viewing session: the key names ``session.uuid``."""
    SESSION_BREAK = "session_break"
    """One answer per session and break (an auction): the key names ``session.uuid`` and the break's event id."""
    GLOBAL = "global"
    """One answer for the whole playback configuration: a constant key."""
    NONE = "none"
    """Never cached: a single-use call (a ticket redeem), which sets no ``Cache``."""


class Cache(ApiShape):
    """``HttpRequestCacheConfiguration``: responses are cached per playback configuration, keyed by ``Key``."""

    ttl_minimum_seconds: int = Field(ge=1)
    ttl_maximum_seconds: int = Field(ge=1)
    key: str | None = None


BUNDLE_ONLY_REQUEST_MEMBERS: Final = ("CacheScope", "CacheReason")
"""``HttpRequestConfiguration`` members the bundle holds for the validators and ``PutFunction`` never receives."""


class HttpRequestConfiguration(ApiShape):
    """``HttpRequestConfiguration``: one HTTP call; its ``Output`` may read ``response``.

    ``cache_scope`` and ``cache_reason`` are the desired state's own: the validators read them, :meth:`ApiShape.api`
    leaves them out of the request.
    """

    runtime: Literal["JSONATA"] = RUNTIME
    method_type: Literal["GET", "POST"]
    url: str
    request_timeout_milliseconds: int
    body: str | None = None
    headers: dict[str, str] | None = None
    output: dict[str, str] | None = None
    cache: Cache | None = None
    cache_scope: CacheScope | None = None
    """What one answer is valid for: ``cache`` and its key follow from it, ``none`` sets no ``cache`` (R-F8)."""
    cache_reason: str | None = None
    """Why that scope is the right one, in words (R-F8)."""

    @model_serializer(mode="wrap")
    def _request(self, handler: SerializerFunctionWrapHandler, info: SerializationInfo) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if info.context and info.context.get(API_CONTEXT):
            for member in BUNDLE_ONLY_REQUEST_MEMBERS:
                data.pop(member, None)
        return data


class CustomOutputConfiguration(ApiShape):
    """``CustomOutputConfiguration``: output bindings evaluated against the session state, no call."""

    runtime: Literal["JSONATA"] = RUNTIME
    output: dict[str, str] | None = None


class FunctionRef(ApiShape):
    """``FunctionRef``: one step of a sequential executor (``RunCondition`` exists only here)."""

    function_id: str
    run_condition: str | None = None
    alias: str | None = None


class SequentialExecutorConfiguration(ApiShape):
    """``SequentialExecutorConfiguration`` (no ``MaxConcurrency``: that is the concurrent executor's)."""

    runtime: Literal["JSONATA"] = RUNTIME
    function_list: tuple[FunctionRef, ...]
    timeout_milliseconds: int
    output: dict[str, str] | None = None


type Configuration = HttpRequestConfiguration | CustomOutputConfiguration | SequentialExecutorConfiguration

_CONFIGURATION_FIELD: Final = {
    FunctionType.HTTP_REQUEST: "http_request_configuration",
    FunctionType.CUSTOM_OUTPUT: "custom_output_configuration",
    FunctionType.SEQUENTIAL_EXECUTOR: "sequential_executor_configuration",
}


class Function(ApiShape):
    """A ``PutFunction`` request: exactly the configuration its ``FunctionType`` names.

    In the bundle ``function_id`` and the executor's step ids are stems; :meth:`released` gives the release's ids.
    """

    function_id: str
    function_type: FunctionType
    description: str | None = None
    http_request_configuration: HttpRequestConfiguration | None = None
    custom_output_configuration: CustomOutputConfiguration | None = None
    sequential_executor_configuration: SequentialExecutorConfiguration | None = None
    tags: dict[str, str] | None = None

    @model_validator(mode="after")
    def _one_configuration(self) -> Self:
        present = sorted(name for name in _CONFIGURATION_FIELD.values() if getattr(self, name) is not None)
        if present != [_CONFIGURATION_FIELD[self.function_type]]:
            expected = to_pascal(_CONFIGURATION_FIELD[self.function_type])
            raise ValueError(f"a {self.function_type} function needs {expected} and no other configuration")
        return self

    @property
    def configuration(self) -> Configuration:
        """The function's one configuration."""
        configuration: Configuration = getattr(self, _CONFIGURATION_FIELD[self.function_type])
        return configuration

    @property
    def output(self) -> dict[str, str]:
        """Its ``Output`` bindings (empty when unset)."""
        return dict(self.configuration.output or {})

    @property
    def steps(self) -> tuple[FunctionRef, ...]:
        """The executor's steps; empty for other types."""
        configuration = self.configuration
        return configuration.function_list if isinstance(configuration, SequentialExecutorConfiguration) else ()

    def released(self, release: int, *, tags: Mapping[str, str] | None = None) -> Function:
        """This document with the ids of ``release``: ``<stem>_rNNNN`` for itself and every step, and ``tags``.

        Raises:
            ValueError: an id already carries a release suffix (it is not a stem).
        """
        rename = {ref.function_id: released_id(ref.function_id, release) for ref in self.steps}
        update: dict[str, Any] = {"function_id": released_id(self.function_id, release), "tags": dict(tags or {})}
        if isinstance(executor := self.configuration, SequentialExecutorConfiguration):
            steps = tuple(
                ref.model_copy(update={"function_id": rename[ref.function_id]}) for ref in executor.function_list
            )
            update["sequential_executor_configuration"] = executor.model_copy(update={"function_list": steps})
        return self.model_copy(update=update)

    def put_request(self, *, tags: bool = True) -> dict[str, Any]:
        """``PutFunction`` keyword arguments. Tags are sent only when the id is created (``tags=True``)."""
        request = self.api()
        if not tags or not request.get("Tags"):
            request.pop("Tags", None)
        return request


# --- FunctionIds ---------------------------------------------------------------------------------------------------


def sibling_scope(prefix: str, configuration: str) -> str:
    """The sibling scope of a playback configuration in FunctionIds: its name without ``<prefix>-``, with ``-`` as
    ``_`` (``fastm-demo-p-a`` → ``demo_p_a``). Channel names hold no ``_``, so the mapping is one to one."""
    return configuration.removeprefix(f"{prefix}-").replace("-", "_")


def function_stem(prefix: str, name: str, *, sibling: str | None = None) -> str:
    """The FunctionId of ``name`` without its release: ``<p>_<name>``, or ``<p>_<scope>_<name>`` for a function that
    embeds the constant of the playback configuration ``sibling``. ``<p>`` is the deployment prefix, which has no
    hyphen (``core.naming.PREFIX_PATTERN``)."""
    scope = f"_{sibling_scope(prefix, sibling)}" if sibling else ""
    return f"{prefix}{scope}_{name}"


def release_tag(release: int) -> str:
    """``rNNNN``: the release in FunctionIds, the ``X-Fast-Fv`` header and the ``fast:release`` tag."""
    if not 1 <= release <= MAX_RELEASE:
        raise ValueError(f"release must be 1-{MAX_RELEASE}, got {release}")
    return f"r{release:04d}"


def released_id(stem: str, release: int) -> str:
    """The FunctionId of ``stem`` in ``release``."""
    if RELEASE_SUFFIX.search(stem):
        raise ValueError(f"{stem!r} already carries a release; pass the stem")
    return f"{stem}_{release_tag(release)}"


# --- builders ------------------------------------------------------------------------------------------------------

SESSION_CONTROLS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "aws.availSuppressionMode": "BEHIND_LIVE_EDGE",
        "aws.availSuppressionValue": "00:00:00",
        "aws.reportingMode": "server",
        "aws.logMode": "DISABLED",
        "aws.preroll": "disabled",
    }
)
"""What PRE_SESSION writes for every monetized session (DESIGN §5.3, ADR-0021). Mid-break joiners get no ads for that
break (suppression mode and value together, P0-25); beacons are server-side; no DEBUG logging; no pre-roll."""


def literal(text: str) -> str:
    """``text`` as a JSONata string-literal expression (``{% 'BEHIND_LIVE_EDGE' %}``), the form P0-05 verified."""
    if "'" in text or "\\" in text:
        raise ValueError(f"literal {text!r} may not contain a quote or a backslash")
    return f"{{% '{text}' %}}"


def reexport(key: str) -> str:
    """The executor ``Output`` value that commits ``key`` as an earlier step wrote it (``{% aws.logMode %}``).
    An executor with an ``Output`` discards every binding it does not reference (P0-05)."""
    return f"{{% {key} %}}"


def ps_ctl(prefix: str) -> Function:
    """``<p>_ps_ctl``: the deployment-wide ``CUSTOM_OUTPUT`` that writes :data:`SESSION_CONTROLS` as literals. It reads
    nothing, ``player_params`` included."""
    return Function(
        function_id=function_stem(prefix, "ps_ctl"),
        function_type=FunctionType.CUSTOM_OUTPUT,
        description="Session controls: mid-break joins see slate, server-side beacons, no debug log, no pre-roll.",
        custom_output_configuration=CustomOutputConfiguration(
            output={key: literal(value) for key, value in SESSION_CONTROLS.items()}
        ),
    )


def sequential_executor(
    stem: str, *, steps: Sequence[FunctionRef], output: Mapping[str, str], timeout_ms: int, description: str
) -> Function:
    """A ``SEQUENTIAL_EXECUTOR`` running ``steps`` in order and committing exactly ``output``."""
    return Function(
        function_id=stem,
        function_type=FunctionType.SEQUENTIAL_EXECUTOR,
        description=description,
        sequential_executor_configuration=SequentialExecutorConfiguration(
            function_list=tuple(steps), timeout_milliseconds=timeout_ms, output=dict(output)
        ),
    )
