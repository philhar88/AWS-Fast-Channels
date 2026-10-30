"""Function documents, FunctionIds and the session controls; the models against botocore's MediaTailor model."""

import types
import typing
from typing import Any, Literal, get_args, get_origin

import botocore.session
import pytest
from pydantic import BaseModel, ValidationError

from fast_channels.core.monetization import functions as fn
from fast_channels.core.monetization import playback as pb
from fast_channels.core.monetization.functions import (
    SESSION_CONTROLS,
    CustomOutputConfiguration,
    Function,
    FunctionRef,
    FunctionType,
    Hook,
    HttpRequestConfiguration,
    SequentialExecutorConfiguration,
)
from fast_channels.core.monetization.jsonata_allowlist import ALLOWED_FUNCTIONS, ALLOWLIST_DATE

MEDIATAILOR = botocore.session.get_session().get_service_model("mediatailor")


def test_the_allow_list_has_the_documented_44_functions() -> None:
    assert len(ALLOWED_FUNCTIONS) == 44
    assert ALLOWLIST_DATE == "2026-10-27"
    assert "pad" not in ALLOWED_FUNCTIONS  # P0-20: accepted by PutFunction, refused at run time


def test_function_ids() -> None:
    assert fn.sibling_scope("fastm", "fastm-demo-p-a") == "demo_p_a"
    assert fn.function_stem("fastm", "ps_ctl") == "fastm_ps_ctl"
    assert fn.function_stem("fastm", "ps_redeem", sibling="fastm-demo-p-a") == "fastm_demo_p_a_ps_redeem"
    assert fn.release_tag(1) == "r0001"
    assert fn.release_tag(9999) == "r9999"
    for release in (0, 10_000):
        with pytest.raises(ValueError, match="release must be 1-9999"):
            fn.release_tag(release)
    assert fn.released_id("fastm_ps_ctl", 2) == "fastm_ps_ctl_r0002"
    with pytest.raises(ValueError, match="already carries a release"):
        fn.released_id("fastm_ps_ctl_r0002", 3)
    assert [h.abbreviation for h in Hook] == ["ps", "pa", "po", "pm"]


def test_session_controls_are_literals_that_read_nothing() -> None:
    ctl = fn.ps_ctl("fastm")
    assert ctl.function_id == "fastm_ps_ctl"
    assert ctl.function_type is FunctionType.CUSTOM_OUTPUT
    assert ctl.output == {
        "aws.availSuppressionMode": "{% 'BEHIND_LIVE_EDGE' %}",
        "aws.availSuppressionValue": "{% '00:00:00' %}",
        "aws.reportingMode": "{% 'server' %}",
        "aws.logMode": "{% 'DISABLED' %}",
        "aws.preroll": "{% 'disabled' %}",
    }
    assert dict(SESSION_CONTROLS)["aws.availSuppressionMode"] == "BEHIND_LIVE_EDGE"
    assert ctl.steps == ()
    for text in ("it's", "back\\slash"):
        with pytest.raises(ValueError, match="quote or a backslash"):
            fn.literal(text)
    assert fn.reexport("aws.logMode") == "{% aws.logMode %}"


def test_a_function_has_exactly_the_configuration_of_its_type() -> None:
    output = CustomOutputConfiguration(output={"temp.x": "{% 'x' %}"})
    with pytest.raises(ValidationError, match="needs HttpRequestConfiguration and no other"):
        Function(function_id="fastm_x", function_type=FunctionType.HTTP_REQUEST, custom_output_configuration=output)
    with pytest.raises(ValidationError, match="needs CustomOutputConfiguration"):
        Function(function_id="fastm_x", function_type=FunctionType.CUSTOM_OUTPUT)
    with pytest.raises(ValidationError, match="MaxConcurrency"):  # the concurrent executor's member
        SequentialExecutorConfiguration.model_validate(
            {"FunctionList": [{"FunctionId": "a"}], "TimeoutMilliseconds": 100, "MaxConcurrency": 2}
        )
    with pytest.raises(ValidationError, match="RunCondition"):  # only a FunctionRef has one
        CustomOutputConfiguration.model_validate({"Output": {}, "RunCondition": "{% true %}"})
    assert Function(function_id="fastm_x", function_type="CUSTOM_OUTPUT", custom_output_configuration=output).output


def test_released_documents_get_release_ids_and_tags() -> None:
    executor = fn.sequential_executor(
        "fastm_ps_root",
        steps=[FunctionRef(function_id="fastm_ps_ctl", run_condition="{% true %}")],
        output={"aws.logMode": "{% aws.logMode %}"},
        timeout_ms=400,
        description="root",
    )
    released = executor.released(7, tags={"fast:release": "r0007"})
    assert released.function_id == "fastm_ps_root_r0007"
    assert [s.function_id for s in released.steps] == ["fastm_ps_ctl_r0007"]
    assert released.steps[0].run_condition == "{% true %}"
    assert released.put_request() == {
        "FunctionId": "fastm_ps_root_r0007",
        "FunctionType": "SEQUENTIAL_EXECUTOR",
        "Description": "root",
        "SequentialExecutorConfiguration": {
            "Runtime": "JSONATA",
            "FunctionList": [{"FunctionId": "fastm_ps_ctl_r0007", "RunCondition": "{% true %}"}],
            "TimeoutMilliseconds": 400,
            "Output": {"aws.logMode": "{% aws.logMode %}"},
        },
        "Tags": {"fast:release": "r0007"},
    }
    assert "Tags" not in released.put_request(tags=False)  # PutFunction with Tags on an existing id fails (W-2 1.16)
    assert "Tags" not in fn.ps_ctl("fastm").released(1).put_request()
    assert executor.function_id == "fastm_ps_root"  # the bundle's document is unchanged
    with pytest.raises(ValueError, match="already carries a release"):
        released.released(8)


def test_api_shapes_use_the_wire_names() -> None:
    http = HttpRequestConfiguration.model_validate(
        {
            "MethodType": "GET",
            "Url": "https://x",
            "RequestTimeoutMilliseconds": 250,
            "Cache": {"TtlMinimumSeconds": 1, "TtlMaximumSeconds": 2},
        }
    )
    assert http.api() == {
        "Runtime": "JSONATA",
        "MethodType": "GET",
        "Url": "https://x",
        "RequestTimeoutMilliseconds": 250,
        "Cache": {"TtlMinimumSeconds": 1, "TtlMaximumSeconds": 2},
    }
    with pytest.raises(ValidationError, match="Extra inputs"):
        HttpRequestConfiguration.model_validate(
            {"MethodType": "GET", "Url": "x", "RequestTimeoutMilliseconds": 1, "Tls": 1}
        )
    with pytest.raises(ValidationError, match="greater than or equal to 1"):  # botocore __integerMin1
        fn.Cache(ttl_minimum_seconds=0, ttl_maximum_seconds=1)


# --- parity with botocore (1.43.114: the model the design was verified against) -----------------------------------


def _models(annotation: Any) -> list[type[BaseModel]]:
    """The pydantic models an annotation holds (``X | None``, ``tuple[X, ...]``)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    return [m for arg in get_args(annotation) for m in _models(arg)]


def _literals(annotation: Any) -> set[str]:
    if get_origin(annotation) is Literal:
        return {str(a) for a in get_args(annotation)}
    if get_origin(annotation) in {types.UnionType, typing.Union, tuple}:
        return {v for arg in get_args(annotation) for v in _literals(arg)}
    return set()


def _assert_matches(model: type[BaseModel], shape: Any, path: str, skip: frozenset[str] = frozenset()) -> None:
    members = shape.members
    for name, info in model.model_fields.items():
        alias = info.alias or name
        if alias in skip or (model is fn.HttpRequestConfiguration and alias in fn.BUNDLE_ONLY_REQUEST_MEMBERS):
            continue
        assert alias in members, f"{path}.{alias} is not a botocore member"
        member = members[alias]
        target = member.member if member.type_name == "list" else member
        if enum := getattr(target, "enum", None):
            assert _literals(info.annotation) <= set(enum), f"{path}.{alias} values"
        for nested in _models(info.annotation):
            _assert_matches(nested, target, f"{path}.{alias}")
    always = {
        info.alias or n for n, info in model.model_fields.items() if info.is_required() or info.default is not None
    }
    assert set(shape.required_members) - skip <= always, f"{path}: botocore requires members the model may omit"


def test_function_models_match_put_function() -> None:
    request = MEDIATAILOR.operation_model("PutFunction").input_shape
    _assert_matches(Function, request, "PutFunction")
    assert {t.value for t in FunctionType} <= set(request.members["FunctionType"].enum)


def test_playback_models_match_put_playback_configuration() -> None:
    request = MEDIATAILOR.operation_model("PutPlaybackConfiguration").input_shape
    _assert_matches(pb.PlaybackConfiguration, request, "PutPlaybackConfiguration", skip=frozenset({"LogConfiguration"}))
    assert {h.value for h in Hook} == set(request.members["FunctionMapping"].key.enum)
    logs = MEDIATAILOR.operation_model("ConfigureLogsForPlaybackConfiguration").input_shape
    _assert_matches(pb.LogConfiguration, logs, "ConfigureLogs", skip=frozenset({"PlaybackConfigurationName"}))
