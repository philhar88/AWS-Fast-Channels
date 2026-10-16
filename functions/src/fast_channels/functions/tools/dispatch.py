"""Lambda entry point shared by every AgentCore Gateway tool target.

The Gateway invokes the target's Lambda with the tool arguments as the event and identifies the tool in
``context.client_context.custom["bedrockAgentCoreToolName"]`` as ``<target>___<tool>``. Failures never raise: every
exception becomes an error envelope the agent can act on (``{"ok": false, "error": {code, message, retryable,
hint}}``); see :func:`as_tool_error`.

Audit trail. The Gateway validates the caller's JWT and authorizes the call (Cedar), but passes no caller identity
to the Lambda: the client context carries only the Gateway's request id, the MCP message id and the gateway and
target ids (gateway-add-target-lambda.html), and the Gateway's own logs name no principal either (checked on a
deployed stage). Every log line of a call carries those as
``gateway_request_id``, ``mcp_message_id``, ``gateway_id`` and ``gateway_target_id``, next to the X-Ray trace id,
and each call of a write or destructive tool logs one ``tool audit`` record with its arguments and outcome. The
Gateway's ``APPLICATION_LOGS`` vended log group is the record of the request itself (``request_id`` =
``gateway_request_id``, ``trace_id`` = the X-Ray trace id). For agent runs that trace id also marks the Runtime's
``run started`` record, which names the actor its JWT authenticated. Correlation and trace ids are chosen by the
caller, though: they link a call to a conversation, but a caller of the Gateway itself can pick any, so they prove
nothing about who made it.
"""

from collections.abc import Callable, Mapping
from typing import Any, Final

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from fast_channels.functions.tools.registry import (
    CORRELATION_FIELD,
    SEPARATOR,
    Access,
    ToolContext,
    ToolError,
    ToolSpec,
    load,
)

logger = Logger()
metrics = Metrics()

_AWS_ERRORS: Final = {
    "ThrottlingException": ("THROTTLED", True),
    "TooManyRequestsException": ("THROTTLED", True),
    "ConflictException": ("CONFLICT", True),
    "ConditionalCheckFailedException": ("CONFLICT", True),
    "NotFoundException": ("NOT_FOUND", False),
    "ResourceNotFoundException": ("NOT_FOUND", False),
    "AccessDeniedException": ("FORBIDDEN", False),
    "BadRequestException": ("VALIDATION", False),
    "ServiceQuotaExceededException": ("QUOTA", False),
}
_GATEWAY_KEYS: Final = {
    "gateway_request_id": "bedrockAgentCoreAwsRequestId",
    "mcp_message_id": "bedrockAgentCoreMcpMessageId",
    "gateway_id": "bedrockAgentCoreGatewayId",
    "gateway_target_id": "bedrockAgentCoreTargetId",
}
_RETRY_HINT: Final = "Retry after a short wait."
_DEFECT_HINT: Final = "This is a fault in the tool or its deployment, not in the arguments: report it to an operator."

type LambdaHandler = Callable[[dict[str, Any], LambdaContext], dict[str, Any]]


def handler(target: str) -> LambdaHandler:
    """Build the Lambda handler for ``target`` (``handler = dispatch.handler("schedule")`` in the target module)."""
    tools = load(target)

    @metrics.log_metrics
    @logger.inject_lambda_context(clear_state=True)
    def handle(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
        gateway = _gateway_context(context)
        qualified = str(gateway.get("bedrockAgentCoreToolName", ""))
        arguments = dict(event)
        correlation_id = arguments.pop(CORRELATION_FIELD, None) or context.aws_request_id
        logger.set_correlation_id(correlation_id)
        logger.append_keys(tool=qualified, **{key: gateway.get(name) for key, name in _GATEWAY_KEYS.items()})

        spec = tools.get(qualified.partition(SEPARATOR)[2])
        if spec is None:
            logger.warning("unknown tool")
            return ToolError("NOT_FOUND", f"unknown tool {qualified}").envelope()
        try:
            payload = spec.input.model_validate(arguments)
        except ValidationError as error:
            failure = ToolError("VALIDATION", _validation_message(error), hint="Fix the arguments and retry.")
            return _failed(spec, arguments, failure)
        try:
            ctx = ToolContext(tool=qualified, correlation_id=correlation_id, invocation=context)
            output = spec.handler(payload, ctx).model_dump(mode="json", exclude_none=True)
        except Exception as error:  # the envelope contract: a tool call never raises
            failure = as_tool_error(error)
            if not isinstance(error, ToolError):
                logger.exception("tool raised", extra={"code": failure.code})
            return _failed(spec, arguments, failure)
        metrics.add_dimension(name="tool", value=qualified)
        metrics.add_metric(name="ToolSucceeded", unit=MetricUnit.Count, value=1)
        logger.info("tool succeeded")
        _audit(spec, arguments, None)
        return {"ok": True, **output}

    return handle


def as_tool_error(error: Exception) -> ToolError:
    """The envelope error for an exception a tool raised.

    AWS errors map by code (throttling and conflicts are retryable); timeouts and transport errors are retryable
    UPSTREAM failures; a pydantic error raised inside a handler (a stored item or an output that does not validate)
    and any other exception are INTERNAL, because changing the arguments would not help.
    """
    match error:
        case ToolError():
            return error
        case ClientError():
            return _from_client_error(error)
        case TimeoutError() | BotoCoreError():
            return ToolError("UPSTREAM", f"{type(error).__name__}: {error}", retryable=True, hint=_RETRY_HINT)
        case ValidationError():
            return ToolError(
                "INTERNAL", f"invalid data inside the tool: {_validation_message(error)}", hint=_DEFECT_HINT
            )
        case _:
            return ToolError("INTERNAL", f"{type(error).__name__}: {error}", hint=_DEFECT_HINT)


def _failed(spec: ToolSpec[Any, Any], arguments: Mapping[str, Any], failure: ToolError) -> dict[str, Any]:
    metrics.add_dimension(name="tool", value=spec.qualified_name)
    metrics.add_metric(name="ToolFailed", unit=MetricUnit.Count, value=1)
    logger.warning("tool failed", extra={"code": failure.code, "error": failure.message})
    _audit(spec, arguments, failure)
    return failure.envelope()


def _audit(spec: ToolSpec[Any, Any], arguments: Mapping[str, Any], failure: ToolError | None) -> None:
    """One ``tool audit`` record per call of a tool that changes something (see the module docstring)."""
    if spec.access is Access.READ:
        return
    audit = {
        "access": spec.access.value,
        "approval": spec.approval.value,
        "arguments": dict(arguments),
        "outcome": "ok" if failure is None else failure.code,
    }
    logger.info("tool audit", extra={"audit": audit})


def _gateway_context(context: LambdaContext) -> Mapping[str, Any]:
    """``client_context.custom`` of a Gateway invocation (empty for any other invoker)."""
    return getattr(context.client_context, "custom", None) or {}


def _validation_message(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in error.errors())


def _from_client_error(error: ClientError) -> ToolError:
    code = error.response.get("Error", {}).get("Code", "")
    message = error.response.get("Error", {}).get("Message", str(error))
    mapped, retryable = _AWS_ERRORS.get(code, ("UPSTREAM", False))
    return ToolError(mapped, f"{code}: {message}", retryable=retryable, hint=_RETRY_HINT if retryable else "")
