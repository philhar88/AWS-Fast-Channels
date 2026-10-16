"""Lambda entry point shared by every AgentCore Gateway tool target.

The Gateway invokes the target's Lambda with the tool arguments as the event and identifies the tool in
``context.client_context.custom["bedrockAgentCoreToolName"]`` as ``<target>___<tool>``. Failures never raise: they are
returned as an error envelope the agent can act on (``{"ok": false, "error": {...}}``).
"""

from collections.abc import Callable
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext
from botocore.exceptions import ClientError
from pydantic import ValidationError

from fast_channels.functions.tools.registry import CORRELATION_FIELD, SEPARATOR, ToolContext, ToolError, load

logger = Logger()
metrics = Metrics()

_AWS_ERRORS = {
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

type LambdaHandler = Callable[[dict[str, Any], LambdaContext], dict[str, Any]]


def handler(target: str) -> LambdaHandler:
    """Build the Lambda handler for ``target`` (``handler = dispatch.handler("schedule")`` in the target module)."""
    tools = load(target)

    @metrics.log_metrics
    @logger.inject_lambda_context(clear_state=True)
    def handle(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
        qualified = _tool_name(context)
        arguments = dict(event)
        correlation_id = arguments.pop(CORRELATION_FIELD, None) or context.aws_request_id
        logger.set_correlation_id(correlation_id)
        logger.append_keys(tool=qualified)

        spec = tools.get(qualified.partition(SEPARATOR)[2])
        if spec is None:
            return ToolError("NOT_FOUND", f"unknown tool {qualified}").envelope()

        try:
            payload = spec.input.model_validate(arguments)
            result = spec.handler(payload, ToolContext(tool=qualified, correlation_id=correlation_id))
        except ValidationError as error:
            failure = ToolError("VALIDATION", _validation_message(error), hint="Fix the arguments and retry.")
        except ToolError as error:
            failure = error
        except ClientError as error:
            failure = _from_client_error(error)
        else:
            metrics.add_dimension(name="tool", value=qualified)
            metrics.add_metric(name="ToolSucceeded", unit=MetricUnit.Count, value=1)
            logger.info("tool succeeded")
            return {"ok": True, **result.model_dump(mode="json", exclude_none=True)}

        metrics.add_dimension(name="tool", value=qualified)
        metrics.add_metric(name="ToolFailed", unit=MetricUnit.Count, value=1)
        logger.warning("tool failed", extra={"code": failure.code, "error": failure.message})
        return failure.envelope()

    return handle


def _tool_name(context: LambdaContext) -> str:
    client_context = context.client_context
    custom = getattr(client_context, "custom", None) or {}
    return str(custom.get("bedrockAgentCoreToolName", ""))


def _validation_message(error: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'input'}: {e['msg']}" for e in error.errors())


def _from_client_error(error: ClientError) -> ToolError:
    code = error.response.get("Error", {}).get("Code", "")
    message = error.response.get("Error", {}).get("Message", str(error))
    mapped, retryable = _AWS_ERRORS.get(code, ("UPSTREAM", False))
    logger.error("AWS call failed", extra={"aws_error": code}, exc_info=error)
    return ToolError(
        mapped, f"{code}: {message}", retryable=retryable, hint="Retry after a short wait." if retryable else ""
    )
