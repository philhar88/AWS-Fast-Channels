"""Fixtures for the monetization desired state: a stage bundle and two contributions shaped like the ones M17 (the
PRE_ADS catalog lookup, DESIGN §9.4) and M4 (the per-sibling ticket redeem, §5.3) will add."""

from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from fast_channels.core.monetization.bundle import (
    Bundle,
    ChannelInput,
    HookTree,
    SiblingContext,
    Step,
    build_bundle,
)
from fast_channels.core.monetization.functions import (
    Cache,
    Function,
    FunctionType,
    Hook,
    HttpRequestConfiguration,
    function_stem,
)
from fast_channels.core.monetization.playback import SiblingRole

if TYPE_CHECKING:
    from fast_channels.core.monetization.validators import Rule, Violation

PREFIX = "fastm"
CHANNEL = "fastm-demo"
CDN = "https://d111111abcdef8.cloudfront.net"
PBS = "https://d222222abcdef8.cloudfront.net"
CA_URL = "https://abc.channel-assembly.mediatailor.us-east-1.amazonaws.com/v1/channel/fastm-demo/"
GRID = (30, 60, 90, 120)
STORED = tuple(f"fast-pod-{s}s" for s in GRID) + tuple(f"fast-pod-kids-{s}s" for s in GRID)

CATALOG_URL = (
    "{% ($u := $string(scte.segmentation_upid); $id := $substring($u, 0, 4) = 'FST3' ? $substring($u, 4) : $u; "
    f"'{CDN}/catalog/ctx/v1/' & $encodeUrlComponent($id) & '.json') %}}"
)


def pa_meta(prefix: str = PREFIX, **http: Any) -> Function:
    """The crawl's cached PRE_ADS lookup (DESIGN §9.4), its header value capped at 1,000 characters."""
    configuration = {
        "method_type": "GET",
        "url": CATALOG_URL,
        "headers": {"Accept": "application/json"},
        "request_timeout_milliseconds": 250,
        "cache": Cache(
            ttl_minimum_seconds=300,
            ttl_maximum_seconds=300,
            key="{% 'meta|v1|' & $string(scte.segmentation_upid) %}",
        ),
        "output": {
            "temp.c_ok": "{% response.statusCode = 200 and response.body.v = 1 %}",
            "temp.c_hdr": "{% $substring($string(response.body.hdr), 0, 1000) %}",
        },
    } | http
    return Function(
        function_id=function_stem(prefix, "pa_meta"),
        function_type=FunctionType.HTTP_REQUEST,
        description="Show-level content metadata by SCTE-35 UPID content id. Reads no session or player data.",
        http_request_configuration=HttpRequestConfiguration(**configuration),
    )


class Contribution:
    """A test contribution: ``build(sibling)`` returns its trees."""

    def __init__(self, name: str, build: Callable[[SiblingContext], Iterable[HookTree]]) -> None:
        self.name = name
        self.build = build

    def hook_trees(self, sibling: SiblingContext) -> Iterable[HookTree]:
        return self.build(sibling)


def crawl(**http: Any) -> Contribution:
    """The PRE_ADS content lookup on the general siblings (not the probe)."""

    def build(sibling: SiblingContext) -> Iterable[HookTree]:
        if sibling.role is SiblingRole.PROBE:
            return []
        step = Step(
            function=pa_meta(sibling.prefix, **http),
            run_condition="{% $exists(scte.segmentation_upid) and scte.segmentation_upid != '' %}",
        )
        return [
            HookTree(
                hook=Hook.PRE_ADS,
                steps=(step,),
                outputs={"adsRequest.headers.x-fast-ctx": "{% temp.c_ok = true ? temp.c_hdr : '' %}"},
                budget_ms=400,
            )
        ]

    return Contribution("crawl", build)


def redeem() -> Contribution:
    """The M4 ticket redeem: an HTTP_REQUEST per sibling, with its playback-configuration name as a constant."""

    def build(sibling: SiblingContext) -> Iterable[HookTree]:
        body = (
            "{% $string({'t': $substring($replace($string(player_params.t), /[^A-Za-z0-9_-]/, ''), 0, 22), "
            f"'sid': session.uuid, 'pc': '{sibling.name}'}}) %}}"
        )
        function = Function(
            function_id=function_stem(sibling.prefix, "ps_redeem", sibling=sibling.name),
            function_type=FunctionType.HTTP_REQUEST,
            http_request_configuration=HttpRequestConfiguration(
                method_type="POST",
                url="https://redeem.example.com/v1/redeem",
                body=body,
                headers={"Content-Type": "application/json"},
                request_timeout_milliseconds=800,
                output={"temp.redeem": "{% response.statusCode = 200 ? 'bound' : 'anon' %}"},
            ),
        )
        return [HookTree(hook=Hook.PRE_SESSION, steps=(Step(function=function),), budget_ms=800)]

    return Contribution("identity", build)


def make_bundle(**overrides: Any) -> Bundle:
    arguments: dict[str, Any] = {
        "prefix": PREFIX,
        "channels": [ChannelInput(name=CHANNEL, video_content_source_url=CA_URL)],
        "content_base_url": CDN,
        "pbs_origin": PBS,
        "log_percent": 100,
        "stored_requests": STORED,
        "grid_seconds": GRID,
    }
    return build_bundle(**(arguments | overrides))


@pytest.fixture
def bundle() -> Bundle:
    return make_bundle()


def edit(bundle: Bundle, change: Callable[[dict[str, Any]], None]) -> Bundle:
    """``bundle`` with its JSON document changed by ``change``, parsed again."""
    document = bundle.model_dump(mode="json", by_alias=True, exclude_none=True)
    change(document)
    return Bundle.model_validate(document)


def function_doc(document: dict[str, Any], stem: str) -> dict[str, Any]:
    """The JSON of function ``stem`` in a bundle document."""
    return next(f for f in document["functions"] if f["FunctionId"] == stem)


def playback_doc(document: dict[str, Any], letter: str = "a") -> dict[str, Any]:
    """The JSON of sibling ``letter``'s playback configuration in a bundle document."""
    return next(s for s in document["siblings"] if s["letter"] == letter)["playback"]


def rules(violations: Iterable[Violation]) -> set[Rule]:
    return {v.rule for v in violations}


def messages(violations: Iterable[Violation], rule: Rule) -> list[str]:
    return [f"{v.where}: {v.message}" for v in violations if v.rule is rule]


@pytest.fixture
def kit() -> SimpleNamespace:
    """The helpers and constants above, for test modules (importlib mode cannot import a conftest by name)."""
    return SimpleNamespace(
        PREFIX=PREFIX,
        CHANNEL=CHANNEL,
        CDN=CDN,
        PBS=PBS,
        CA_URL=CA_URL,
        GRID=GRID,
        STORED=STORED,
        CATALOG_URL=CATALOG_URL,
        pa_meta=pa_meta,
        Contribution=Contribution,
        crawl=crawl,
        redeem=redeem,
        make_bundle=make_bundle,
        edit=edit,
        function_doc=function_doc,
        playback_doc=playback_doc,
        rules=rules,
        messages=messages,
    )
