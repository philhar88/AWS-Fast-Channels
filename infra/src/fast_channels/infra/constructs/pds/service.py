"""The Pod Decision Service's endpoints: an internal load balancer in the Prebid Server VPC, one function per path, and
the census gate that keeps the functions warm while channels run.

- :class:`PdsService`: an internal Application Load Balancer in the guidance's private subnets at
  ``pds.<prefix>.fast.internal`` (the name Prebid Server's configuration has used since the demand module's first
  deploy), HTTP on port 80. Its security group admits only the Prebid Server tasks' security group. Each endpoint is a
  ``POST`` rule to one function's ``live`` alias; any other request gets a 404.
- :class:`PdsFunction`: a :class:`~fast_channels.infra.constructs.functions.FastFunction` outside the VPC (the load
  balancer invokes it; it reaches DynamoDB and S3 at their public endpoints), so it has no network interface cold
  start, and a ``live`` alias.
- :class:`PdsCensusGate`: a JSONata state machine on ``Census Changed``. It sets ``provisioned_concurrency`` on every
  alias when the first channel starts, and removes it 15 minutes after the last one stops if none runs then.

Why in the VPC and warm: ``/policy`` has 80 ms, of which the load balancer's Lambda invoke takes 20-40 ms. Reached over
API Gateway and a NAT gateway, a third of MediaTailor's
auctions fell back to the own book (every first break: a cold function and cold connections).
"""

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from fast_channels.core.demand import SCALE_IN_AFTER_SECONDS
from fast_channels.core.events import DETAIL_TYPES, CensusChanged, event_source
from fast_channels.core.pds.names import ALIAS, RECORD, Endpoint, census_gate, target_group
from fast_channels.infra import nag
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.lambda_target import ScopedLambdaTarget
from fast_channels.infra.grants import allow

if TYPE_CHECKING:
    from collections.abc import Mapping

PORT: Final = 80
TIMEOUT: Final = Duration.seconds(3)
"""Far above any caller's budget (the module waits 80 to 200 ms): the module always gives up first."""
MEMORY_MB: Final = 1769
"""One vCPU: Lambda's CPU scales with memory, and the decision path is CPU-bound (JSON, pydantic, and after an idle
spell the TLS handshake that reopens the connection to DynamoDB). Measured on fastcid (M2-x2): the first ``/select``
after 90-180 s idle took 84-128 ms in the handler at 1,024 MB and 30-94 ms at 1,769 MB."""
IDLE_TIMEOUT: Final = Duration.seconds(120)
"""Above Prebid Server's HTTP client keep-alive (Vert.x's 60 s default; ``http-client.idle-timeout-ms`` is 0), so the
client closes an idle connection first; with both at 60 s a request could race the load balancer's close."""
WHY_LOGS: Final = "Every request is logged by its function as a decision record; the balancer adds nothing to them"


class PdsFunction(Construct):
    """One endpoint's function and its ``live`` alias.

    Args:
        handler: the handler in ``fast_channels.functions.pds``.
        description: what it does.
        environment: its environment variables.
        log_retention, removal_policy: of its log group.

    Attributes:
        function: the :class:`FastFunction`. alias: the ``live`` alias the load balancer invokes.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        handler: str,
        description: str,
        environment: Mapping[str, str],
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.function = FastFunction(
            self,
            "Function",
            compiled=True,  # a cold start is on the module's deadline
            handler=handler,
            description=description,
            environment=environment,
            timeout=TIMEOUT,
            memory_size=MEMORY_MB,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        self.alias = lambda_.Alias(
            self, "Live", alias_name=ALIAS, version=self.function.function.current_version, description="Served"
        )
        nag.acknowledge(
            self.function,
            {nag.LAMBDA_DLQ: "Invoked synchronously by the load balancer; a failure is the caller's fallback"},
        )


class PdsService(Construct):
    """The internal load balancer, its listener and its record (module docstring).

    Args:
        prefix: the stage's prefix.
        vpc: the guidance's VPC.
        task_security_group_id: the Prebid Server tasks' security group, the only source admitted.
        zone: the stage's private zone (``<prefix>.fast.internal``).

    Attributes:
        load_balancer: the load balancer. listener: its HTTP listener. record: ``pds.<prefix>.fast.internal``.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        vpc: ec2.IVpc,
        task_security_group_id: str,
        zone: route53.IHostedZone,
    ) -> None:
        super().__init__(scope, id)
        security_group = ec2.SecurityGroup(
            self,
            "SecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="FAST Channels: the Pod Decision Service load balancer, from Prebid Server tasks only",
        )
        tasks = ec2.SecurityGroup.from_security_group_id(self, "Tasks", task_security_group_id, mutable=False)
        security_group.add_ingress_rule(tasks, ec2.Port.tcp(PORT), "Prebid Server tasks")
        self.load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=vpc,
            internet_facing=False,
            security_group=security_group,
            vpc_subnets=ec2.SubnetSelection(subnets=vpc.private_subnets),
            drop_invalid_header_fields=True,
            idle_timeout=IDLE_TIMEOUT,
        )
        self.listener = self.load_balancer.add_listener(
            "Http",
            port=PORT,
            open=False,
            default_action=elbv2.ListenerAction.fixed_response(
                404, content_type="application/json", message_body='{"error":"not found"}'
            ),
        )
        self.record = route53.ARecord(
            self,
            "Record",
            zone=zone,
            record_name=RECORD,
            target=route53.RecordTarget.from_alias(route53_targets.LoadBalancerTarget(self.load_balancer)),
        )
        nag.acknowledge(self.load_balancer, {"AwsSolutions-ELB2": WHY_LOGS})
        self._prefix = prefix
        self._priority = 0

    def add_endpoint(self, endpoint: Endpoint, function: PdsFunction) -> elbv2.ApplicationTargetGroup:
        """Route ``POST <endpoint>`` to ``function``'s ``live`` alias."""
        self._priority += 10
        name = target_group(self._prefix, endpoint)
        group = elbv2.ApplicationTargetGroup(
            self,
            f"Target{endpoint.function_suffix.title()}",
            target_group_name=name,
            targets=[ScopedLambdaTarget(function.alias, name)],
        )
        self.listener.add_target_groups(
            f"Route{endpoint.function_suffix.title()}",
            priority=self._priority,
            conditions=[
                elbv2.ListenerCondition.path_patterns([endpoint.value]),
                elbv2.ListenerCondition.http_request_methods(["POST"]),
            ],
            target_groups=[group],
        )
        return group


def census_gate_definition(*, functions: Sequence[str], parameter: str, concurrency: int) -> dict[str, Any]:
    """The gate's JSONata state machine: provisioned concurrency on the ``live`` alias of each function in
    ``functions`` (ARNs) while any channel runs. With ``concurrency`` 0 every event releases it, so turning it off
    also removes what an earlier configuration set.

    Overlapping executions are possible (a stop, a start and a stop within 15 minutes): the first stop's execution
    then releases 15 minutes after it, while no channel runs, which only shortens the grace period. A channel that
    starts between a release's census read and its delete runs cold until the next census change.
    """
    retry = [{"ErrorEquals": ["States.ALL"], "IntervalSeconds": 5, "MaxAttempts": 3, "BackoffRate": 2}]
    absent = ["Lambda.ResourceNotFoundException", "Lambda.ProvisionedConcurrencyConfigNotFoundException"]
    states: dict[str, Any] = {
        "AnyChannel": {
            "Type": "Choice",
            "Choices": [
                {
                    "Condition": "{% $states.input.detail.running > 0 %}",
                    "Next": "Provision0" if concurrency else "Release0",
                }
            ],
            "Default": "WaitBeforeRelease" if concurrency else "Release0",
        },
        "WaitBeforeRelease": {"Type": "Wait", "Seconds": SCALE_IN_AFTER_SECONDS, "Next": "StillNoChannel"},
        "StillNoChannel": {
            "Type": "Task",
            "Resource": "arn:aws:states:::aws-sdk:ssm:getParameter",
            "Arguments": {"Name": parameter},
            "Assign": {"running": "{% $number($states.result.Parameter.Value) %}"},
            "Retry": retry,
            "Next": "NoneRunning",
        },
        "NoneRunning": {
            "Type": "Choice",
            "Choices": [{"Condition": "{% $running = 0 %}", "Next": "Release0"}],
            "Default": "ChannelsBack",
        },
        "ChannelsBack": {"Type": "Succeed"},
    }
    for index, function in enumerate(functions):
        last = index == len(functions) - 1
        if concurrency:
            states[f"Provision{index}"] = {
                "Type": "Task",
                "Resource": "arn:aws:states:::aws-sdk:lambda:putProvisionedConcurrencyConfig",
                "Arguments": {
                    "FunctionName": function,
                    "Qualifier": ALIAS,
                    "ProvisionedConcurrentExecutions": concurrency,
                },
                "Retry": retry,
                **({"End": True} if last else {"Next": f"Provision{index + 1}"}),
            }
        following = {"End": True} if last else {"Next": f"Release{index + 1}"}
        states[f"Release{index}"] = {
            "Type": "Task",
            "Resource": "arn:aws:states:::aws-sdk:lambda:deleteProvisionedConcurrencyConfig",
            "Arguments": {"FunctionName": function, "Qualifier": ALIAS},
            "Retry": [{**retry[0], "ErrorEquals": ["Lambda.TooManyRequestsException", "Lambda.ServiceException"]}],
            "Catch": [{"ErrorEquals": absent, "Next": f"Released{index}"}],
            **following,
        }
        states[f"Released{index}"] = {"Type": "Pass", **following}
    return {
        "Comment": "Keep the Pod Decision Service's functions warm while channels run",
        "QueryLanguage": "JSONata",
        "StartAt": "AnyChannel",
        "States": states,
    }


class PdsCensusGate(Construct):
    """The census gate for the functions' provisioned concurrency (module docstring), and its rule.

    Args:
        prefix: the stage's prefix (its event bus).
        functions: the PDS functions.
        concurrency: provisioned concurrency per function while channels run (``modules.pds.provisioned_concurrency``).
        census_parameter: the census's last count (``/<prefix>/ops/running-channels``).
        log_retention: the state machine's log retention.
        dead_letters: where undeliverable ``Census Changed`` events go.

    Attributes:
        state_machine: the gate. rule: the ``Census Changed`` rule.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        functions: Sequence[PdsFunction],
        concurrency: int,
        census_parameter: str,
        log_retention: logs.RetentionDays,
        dead_letters: sqs.IQueue,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/vendedlogs/states/{census_gate(prefix)}",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY,
        )
        arns = [function.function.function.function_arn for function in functions]
        definition = census_gate_definition(functions=arns, parameter=census_parameter, concurrency=concurrency)
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_name=census_gate(prefix),
            definition_body=sfn.DefinitionBody.from_string(json.dumps(definition)),
            comment=f"FAST Channels ({prefix}): the PDS stays warm while channels run",
            timeout=Duration.minutes(30),
            tracing_enabled=True,
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=True),
        )
        allow(
            self.state_machine,
            ["lambda:PutProvisionedConcurrencyConfig", "lambda:DeleteProvisionedConcurrencyConfig"],
            [function.alias.function_arn for function in functions],
        )
        allow(
            self.state_machine,
            ["ssm:GetParameter"],
            [stack.format_arn(service="ssm", resource="parameter", resource_name=census_parameter.lstrip("/"))],
        )
        nag.acknowledge_references(
            self.state_machine.role,
            {nag.wildcard_resource(): "X-Ray has no resource-level permissions"},
        )
        bus = events.EventBus.from_event_bus_name(self, "Bus", f"{prefix}-fast-channels")
        self.rule = events.Rule(
            self,
            "CensusChanged",
            event_bus=bus,
            description=f"FAST Channels ({prefix}): the PDS's provisioned concurrency follows the running channels",
            event_pattern=events.EventPattern(source=[event_source(prefix)], detail_type=[DETAIL_TYPES[CensusChanged]]),
            targets=[
                events_targets.SfnStateMachine(self.state_machine, retry_attempts=8, dead_letter_queue=dead_letters)
            ],
        )
