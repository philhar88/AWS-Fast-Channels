"""Lambda functions of the shared ``fast-channels-functions`` code asset, and runtime defaults for every other
Lambda function and state machine in the stage.

``FastFunction`` is how FAST Channels deploys its own handlers. The stage registers the rest so that functions and
state machines created by any other construct (custom-resource providers, ``AwsCustomResource``,
``BucketDeployment``, ...) get the same observability baseline: active tracing, JSON logs and an explicit log group
with retention. ``LambdaDefaults`` and ``StateMachineDefaults`` are property injectors (explicit props always win);
``FunctionLogGroups`` is an aspect for the raw functions of CDK's core custom-resource providers and for
the deletion order of function roles and log groups.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

# jsii resolves the annotations of Python callbacks (``inject``, ``visit``) at runtime: keep their types
# (InjectionContext, IConstruct) as runtime imports, never under TYPE_CHECKING.
import jsii
from aws_cdk import CfnResource, Duration, IAspect, InjectionContext, IPropertyInjector, Names, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct, IConstruct

from fast_channels.infra import nag
from fast_channels.infra.bundling import workspace_code

FUNCTIONS_PACKAGE: Final = "fast-channels-functions"
METRICS_NAMESPACE: Final = "FastChannels"
DEFAULT_TIMEOUT: Final = Duration.seconds(30)
_HANDLER_ROOT: Final = "fast_channels.functions."


class _SharedCode(Construct):
    """Holds the stack's single code asset; an ``AssetCode`` can only be bound within one stack."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        self.code = workspace_code(FUNCTIONS_PACKAGE)

    @classmethod
    def of(cls, scope: Construct) -> lambda_.Code:
        stack = Stack.of(scope)
        holder = stack.node.try_find_child("FastFunctionsCode")
        return (holder if isinstance(holder, cls) else cls(stack, "FastFunctionsCode")).code


class FastFunction(Construct):
    """A FAST Channels Lambda handler: Python 3.14 on arm64 from the shared code asset (one per stack), with
    JSON logging to its own log group, active tracing and recursive-loop termination.

    The execution role only writes to that log group (no managed policy, so late log deliveries during stack
    deletion cannot re-create the group). Active tracing adds ``xray:PutTraceSegments``/``PutTelemetryRecords`` on
    ``*`` (X-Ray has no resource-level permissions) to the role's default policy; that
    ``AwsSolutions-IAM5[Resource::*]`` finding is acknowledged here, so review any other ``*`` grant to
    ``function.role``. cdk-nag's ``Serverless-LambdaDLQ`` remains: acknowledge it with how the invoker handles
    failures, e.g. ``nag.acknowledge(fn, {nag.LAMBDA_DLQ: "Invoked synchronously by Step Functions"})``.

    Args:
        handler: dotted path of the handler in ``fast_channels.functions``, e.g.
            ``"fast_channels.functions.tools.catalog.handler"``; its module path (``"tools.catalog"``) becomes
            ``POWERTOOLS_SERVICE_NAME``.
        description: what the function does (shown in the Lambda console).
        environment: variables the function needs (CONTRACTS §C), added to the Powertools ones.
        timeout: maximum run time.
        memory_size: memory in MB (also scales CPU).
        log_retention: retention of the function's log group.
        removal_policy: what happens to the log group when the stack is deleted.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        handler: str,
        description: str,
        environment: Mapping[str, str] | None = None,
        timeout: Duration = DEFAULT_TIMEOUT,
        memory_size: int = 512,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        if not handler.startswith(_HANDLER_ROOT):
            raise ValueError(f"handler {handler!r} must live in the fast_channels.functions package")
        self.log_group = logs.LogGroup(self, "Logs", retention=log_retention, removal_policy=removal_policy)
        role = iam.Role(self, "Role", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
        self.log_group.grant_write(role)
        self.function = lambda_.Function(
            self,
            "Resource",
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            code=_SharedCode.of(self),
            handler=handler,
            description=description,
            role=role,
            environment={
                "POWERTOOLS_SERVICE_NAME": handler.removeprefix(_HANDLER_ROOT).rpartition(".")[0],
                "POWERTOOLS_METRICS_NAMESPACE": METRICS_NAMESPACE,
                **(environment or {}),
            },
            timeout=timeout,
            memory_size=memory_size,
            logging_format=lambda_.LoggingFormat.JSON,
            application_log_level_v2=lambda_.ApplicationLogLevel.INFO,
            # WARN would drop platform.report (duration, billed and init time) (cdk-standards.md §5).
            system_log_level_v2=lambda_.SystemLogLevel.INFO,
            log_group=self.log_group,
            tracing=lambda_.Tracing.ACTIVE,
            recursive_loop=lambda_.RecursiveLoop.TERMINATE,
        )
        nag.acknowledge(
            role,
            {
                nag.wildcard_resource(): "Active tracing: xray:PutTraceSegments/PutTelemetryRecords only support '*'",
            },
        )


@dataclass(frozen=True, slots=True)
class LogGroupDefaults:
    """Retention and removal policy of the log groups the runtime defaults create for other constructs."""

    retention: logs.RetentionDays
    removal_policy: RemovalPolicy

    def create(self, scope: Construct, id: str, *, name: str | None = None) -> logs.LogGroup:
        """A log group in ``scope``, named by CloudFormation unless ``name`` is given."""
        return logs.LogGroup(
            scope, id, log_group_name=name, retention=self.retention, removal_policy=self.removal_policy
        )


@jsii.implements(IPropertyInjector)
class LambdaDefaults:
    """Defaults for every ``lambda.Function`` in scope: active tracing, JSON logs at INFO, recursive-loop
    termination and, unless the caller passes a log group, an explicit one with retention. Without it, the Lambda
    service (or CDK's managed log group) keeps logs forever or for 731 days and leaves them behind on deletion."""

    def __init__(self, log_groups: LogGroupDefaults) -> None:
        self._log_groups = log_groups

    @property
    def construct_unique_id(self) -> str:
        """Construct type this injector applies to (must be a property, or jsii ignores the injector)."""
        return cast("str", lambda_.Function.PROPERTY_INJECTION_ID)

    def inject(self, original_props: dict[str, Any], context: InjectionContext) -> dict[str, Any]:
        """Return ``original_props`` (camelCase keys) completed with the defaults; explicit props win."""
        props = dict(original_props)
        props.setdefault("tracing", lambda_.Tracing.ACTIVE)
        props.setdefault("recursiveLoop", lambda_.RecursiveLoop.TERMINATE)
        # Log levels require the JSON format; the legacy ``logFormat`` prop excludes ``loggingFormat``.
        json_logs = "logFormat" not in props and props.setdefault("loggingFormat", lambda_.LoggingFormat.JSON)
        if json_logs == lambda_.LoggingFormat.JSON:
            props.setdefault("applicationLogLevelV2", lambda_.ApplicationLogLevel.INFO)
            props.setdefault("systemLogLevelV2", lambda_.SystemLogLevel.INFO)
        if "logGroup" not in props and "logRetention" not in props:
            props["logGroup"] = self._log_groups.create(context.scope, f"{context.id}Logs")
        return props


@jsii.implements(IPropertyInjector)
class StateMachineDefaults:
    """Defaults for every ``sfn.StateMachine`` in scope: X-Ray tracing and, unless the caller configures logging,
    ALL-level logs with execution data to ``/aws/vendedlogs/states/<unique-name>`` (the ``/aws/vendedlogs/``
    prefix keeps the account's CloudWatch Logs resource policy from growing)."""

    def __init__(self, log_groups: LogGroupDefaults) -> None:
        self._log_groups = log_groups

    @property
    def construct_unique_id(self) -> str:
        """Construct type this injector applies to (must be a property, or jsii ignores the injector)."""
        return cast("str", sfn.StateMachine.PROPERTY_INJECTION_ID)

    def inject(self, original_props: dict[str, Any], context: InjectionContext) -> dict[str, Any]:
        """Return ``original_props`` (camelCase keys) completed with the defaults; explicit props win."""
        props = {"tracingEnabled": True, **original_props}
        if "logs" not in props:
            name = f"{Names.unique_resource_name(context.scope, max_length=400, separator='-')}-{context.id}"
            log_group = self._log_groups.create(
                context.scope, f"{context.id}Logs", name=f"/aws/vendedlogs/states/{name}"
            )
            props["logs"] = sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=True)
        return props


@jsii.implements(IAspect)
class FunctionLogGroups:
    """Aspect completing the function log groups of the runtime defaults:

    - functions of CDK's core ``CustomResourceProvider`` (raw ``AWS::Lambda::Function`` resources such as the S3
      auto-delete-objects handler, which property injectors do not reach) get a log group with retention;
    - the execution role of every function whose log group the defaults created is deleted before that log group.
      Otherwise the asynchronous delivery of a function's last logs (custom resources run while their stack is
      deleted) re-creates the group, without retention, after CloudFormation deleted it (observed).
    """

    def __init__(self, log_groups: LogGroupDefaults) -> None:
        self._log_groups = log_groups

    def visit(self, node: IConstruct) -> None:
        """Give raw provider functions a log group; order role and log group deletion."""
        scope = cast("Construct", node.node.scope)
        if isinstance(node, lambda_.Function):
            _delete_before(node.role, scope.node.try_find_child(f"{node.node.id}Logs"))
        elif (
            isinstance(node, CfnResource)
            and not isinstance(node, lambda_.CfnFunction)
            and node.cfn_resource_type == "AWS::Lambda::Function"
        ):
            log_group = self._log_groups.create(scope, f"{node.node.id}Logs")
            node.add_property_override("LoggingConfig.LogGroup", log_group.log_group_name)
            _delete_before(scope.node.try_find_child("Role"), log_group)


def _delete_before(role: IConstruct | None, log_group: IConstruct | None) -> None:
    if isinstance(log_group, logs.LogGroup) and role is not None and Stack.of(role) is Stack.of(log_group):
        role.node.add_dependency(log_group)
