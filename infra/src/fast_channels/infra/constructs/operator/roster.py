"""The agent roster (``modules.agents``; M9, ADR-0023): each agent of the envelope that is on, with its runner.

- ``CapCounters``: the agents' table ``<prefix>-agents``, one item per cap and window (``CAP#...``, ``sk`` = the
  window's start) with a TTL a day after it (``functions.agents.caps``).
- One runner per agent (``fast_channels.functions.agents.runner``): it authenticates as the agent's machine client
  (``OperatorIdentity(agents=...)``), holds the agent's unattended conversations with the scheduler agent's Runtime,
  answers its approval-gated calls from the envelope and a read of the caps, appends every decision and tool call
  to the ledger chain ``agents/<role>`` and escalates refusals (and a propose-only agent's proposals) to the alert
  topic.
- The Gateway's request interceptor (``fast_channels.functions.agents.interceptor``): every non-READ tool call an
  agent's token makes is checked against the envelope at the Gateway, takes its caps there, and is recorded; one
  outside the envelope is refused before it reaches the tool, and escalated.
- The operations agent's triggers (``functions.agents.operations``): the census's ``Census Changed`` to zero
  running channels on the stage's bus, and the probe canary's off-air alarm (runbook R12's trigger) going to
  ALARM. Its runner reads the always-on channels' states first, and starts a conversation only for a stopped one.
- The programming agent's trigger (``functions.agents.programming``, R6), with programmed channels: an hourly rule.
  Its runner reads the channels' schedules first, and starts a conversation only for one that ends within 6 hours.
- The sales agent's trigger (``functions.agents.sales``), with ``modules.pds``: an hourly rule. Its runner reads the
  own book's pacing first, and starts a conversation only for a line that is off pace.
- The envelope's record (``fast_channels.functions.agents.envelope_record``), run on every deploy that changes the
  envelope: ``envelope.apply`` in the ledger chain ``agents/envelope``.

The Gateway's Cedar policies for the agents' principals come from the same envelope (``AgentGateway(agents=...)``).
"""

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Duration, RemovalPolicy, triggers
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_lambda_destinations as destinations
from constructs import Construct

from fast_channels.core.agents.envelope import ENVELOPE_CHAIN, Envelope, Role
from fast_channels.core.agents.names import table_name
from fast_channels.core.events import DETAIL_TYPES, CensusChanged, event_source
from fast_channels.core.ledger import ledger_table
from fast_channels.functions.media_library import CHANNEL_PREFIX
from fast_channels.functions.tools.registry import Access, Approval, ToolSpec
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.alerts import PERIOD, alarm, dead_letter_alarm
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.operator.identity import AGENT_SCOPE, OperatorIdentity
from fast_channels.infra.constructs.operator.tools import OwnBook, grant_own_book_reads
from fast_channels.infra.constructs.queues import dead_letter_queue

if TYPE_CHECKING:
    from aws_cdk import aws_logs as logs
    from aws_cdk import aws_sns as sns
    from aws_cdk import aws_sqs as sqs

RUNNER_HANDLER: Final = "fast_channels.functions.agents.runner.handler"
RECORD_HANDLER: Final = "fast_channels.functions.agents.envelope_record.handler"
INTERCEPTOR_HANDLER: Final = "fast_channels.functions.agents.interceptor.handler"
INTERCEPTOR_TIMEOUT: Final = Duration.seconds(15)
"""The interceptor reads a plan, takes the caps and records the call: a few DynamoDB requests."""
TICKS: Final = {Role.PROGRAMMING: "0", Role.SALES: "30"}
"""The minute of each hour the programming and sales agents' rules fire."""
RUNNER_TIMEOUT: Final = Duration.minutes(15)
"""A run reads, decides and acts in a few model calls; the runner stops resuming a minute before the timeout."""
RETRY_ATTEMPTS: Final = 1
MAX_EVENT_AGE: Final = Duration.hours(1)


class CapCounters(dynamodb.TableV2):
    """``<prefix>-agents``: the envelope caps' counters (module docstring). On demand, no point-in-time recovery: a
    lost counter only forgets part of one hour's count."""

    def __init__(self, scope: Construct, id: str, *, prefix: str, removal_policy: RemovalPolicy) -> None:
        super().__init__(
            scope,
            id,
            table_name=table_name(prefix),
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            time_to_live_attribute="ttl",
            removal_policy=removal_policy,
        )
        nag.acknowledge(
            self,
            {"AwsSolutions-DDB3": "Hourly cap counters: a restore has no value, a lost item forgets one window"},
        )


class AgentRoster(Construct):
    """The roster's runners, their caps' table and the envelope's ledger record (module docstring).

    Args:
        prefix: the deployment prefix.
        envelope: the envelope, resolved for the deployment.
        identity: the operator plane's Cognito pool, with a machine client per agent that is on.
        runtime_invoke_url: the scheduler agent Runtime's invocation URL.
        alert_topic: where refusals are escalated (the stage's alert topic).
        event_bus: the stage's bus (the census's events).
        off_air_alarm: the probe canary's alarm (runbook R12's trigger).
        gateway: the Gateway whose tool calls the interceptor checks.
        tools: every tool of the Gateway's targets.
        library_table: the media library table (the stored plans a publish lead reads).
        own_book: the PDS's own book, with the sales agent (``modules.pds``).
        log_retention, removal_policy: of the functions' log groups, the queues and the table.

    Attributes:
        table: the caps' counters. runners: each agent's runner, by role. dead_letters: each runner's queue.
        interceptor: the Gateway's request interceptor. record: the envelope's record function. alarms: by name.

    Raises:
        ValueError: an agent that is on has no machine client, or the sales agent is on without ``own_book``.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        envelope: Envelope,
        identity: OperatorIdentity,
        runtime_invoke_url: str,
        alert_topic: sns.ITopic,
        event_bus: events.IEventBus,
        off_air_alarm: str,
        gateway: agentcore.Gateway,
        tools: Iterable[ToolSpec[Any, Any]],
        library_table: str,
        own_book: OwnBook | None = None,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        roles = envelope.enabled()
        if missing := [role.value for role in roles if role not in identity.agent_clients]:
            raise ValueError(f"agents {missing} need their machine clients (OperatorIdentity(agents=...))")
        if Role.SALES in roles and own_book is None:
            raise ValueError("the sales agent paces the PDS own book: AgentRoster(own_book=...)")
        self._prefix = prefix
        self.table = CapCounters(self, "Caps", prefix=prefix, removal_policy=removal_policy)
        document = envelope.document
        self.runners: dict[Role, FastFunction] = {}
        self.dead_letters: dict[Role, sqs.Queue] = {}
        self.alarms: dict[str, cw.Alarm] = {}
        for role in roles:
            runner = FastFunction(
                self,
                f"{role.value.title()}Runner",
                handler=RUNNER_HANDLER,
                description=f"Agents: the {role.value} agent's unattended runs inside the autonomy envelope",
                environment={
                    "FAST_PREFIX": prefix,
                    "ROLE": role.value,
                    "RUNTIME_INVOKE_URL": runtime_invoke_url,
                    "USER_POOL_ID": identity.user_pool.user_pool_id,
                    "AGENT_CLIENT_ID": identity.agent_clients[role].user_pool_client_id,
                    "TOKEN_ENDPOINT": identity.token_endpoint,
                    "SCOPE": AGENT_SCOPE,
                    "ENVELOPE": document,
                    "AGENTS_TABLE": table_name(prefix),
                    "LIBRARY_TABLE": library_table,
                    "LEDGER_TABLE": ledger_table(prefix),
                    "ALERT_TOPIC_ARN": alert_topic.topic_arn,
                    **(
                        {"PDS_TABLE": own_book.table, "PDS_CONFIG_BUCKET": own_book.config_bucket}
                        if role is Role.SALES and own_book
                        else {}
                    ),
                },
                timeout=RUNNER_TIMEOUT,
                log_retention=log_retention,
                removal_policy=removal_policy,
            )
            queue = dead_letter_queue(self, f"{role.value.title()}DeadLetters", removal_policy=removal_policy)
            runner.function.configure_async_invoke(
                retry_attempts=RETRY_ATTEMPTS,
                max_event_age=MAX_EVENT_AGE,
                on_failure=destinations.SqsDestination(queue),
            )
            nag.acknowledge(
                runner,
                {nag.LAMBDA_DLQ: "Asynchronous failures go to the dead-letter queue as the on-failure destination"},
            )
            identity.user_pool.grant(runner.function, "cognito-idp:DescribeUserPoolClient")
            runner.allow(  # it reads the caps; the interceptor takes them
                ["dynamodb:GetItem"],
                [arn(self, "dynamodb", "table", table_name(prefix))],
                conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"CAP#{role.value}*"]}},
            )
            _allow_ledger(runner, prefix, role.chain_id)
            alert_topic.grant_publish(runner.function)
            match role:
                case Role.OPERATIONS:
                    self._operations(prefix, envelope, runner, event_bus, off_air_alarm)
                case Role.PROGRAMMING:
                    self._programming(envelope, runner, library_table)
                case Role.SALES if own_book is not None:
                    grant_own_book_reads(runner, own_book)
                    self._tick(role, runner, "the sales agent looks at the own book's pacing")
            self.runners[role] = runner
            self.dead_letters[role] = queue
            self._alarm_on(prefix, role, runner, queue, alert_topic)
        self.interceptor = self._interceptor(
            envelope,
            identity=identity,
            roles=roles,
            tools=tools,
            library_table=library_table,
            alert_topic=alert_topic,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        gateway.add_interceptor(
            agentcore.LambdaInterceptor.for_request(self.interceptor.function, pass_request_headers=True)
        )
        nag.acknowledge_references(
            gateway.role,
            {
                nag.wildcard_resource(f"{self.interceptor.function.function_arn}:*"): "Gateway L2 grant: invoke "
                "the request interceptor and its versions"
            },
        )
        self.record = FastFunction(
            self,
            "EnvelopeRecord",
            handler=RECORD_HANDLER,
            description="Agents: records the deployed autonomy envelope's version in the ledger",
            environment={"FAST_PREFIX": prefix, "ENVELOPE": document, "LEDGER_TABLE": ledger_table(prefix)},
            timeout=Duration.seconds(30),
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        _allow_ledger(self.record, prefix, ENVELOPE_CHAIN)
        nag.acknowledge(
            self.record, {nag.LAMBDA_DLQ: "Invoked synchronously by the deploy trigger: a failure fails the deploy"}
        )
        triggers.Trigger(
            self,
            "OnEnvelope",
            handler=self.record.function,
            invocation_type=triggers.InvocationType.REQUEST_RESPONSE,
            timeout=Duration.minutes(1),
            execute_on_handler_change=True,
        )

    def _operations(
        self,
        prefix: str,
        envelope: Envelope,
        runner: FastFunction,
        event_bus: events.IEventBus,
        off_air_alarm: str,
    ) -> None:
        """The operations agent's triggers and its reads of the always-on channels' states."""
        if envelope.always_on:
            runner.allow(
                ["mediatailor:DescribeChannel"],
                [arn(self, "mediatailor", "channel", name) for name in envelope.always_on],
            )
        target = events_targets.LambdaFunction(runner.function, retry_attempts=2, max_event_age=MAX_EVENT_AGE)
        for rule in (
            events.Rule(
                self,
                "OperationsCensusTrigger",
                description="Agents: the operations agent looks at the always-on channels when none runs",
                event_bus=event_bus,
                event_pattern=events.EventPattern(
                    source=[event_source(prefix)],
                    detail_type=[DETAIL_TYPES[CensusChanged]],
                    detail={"running": [0]},
                ),
                targets=[target],
            ),
            events.Rule(
                self,
                "OperationsOffAirTrigger",
                description="Agents: the operations agent looks at the always-on channels when the probe is off air",
                event_pattern=events.EventPattern(
                    source=["aws.cloudwatch"],
                    detail_type=["CloudWatch Alarm State Change"],
                    detail={"alarmName": [off_air_alarm], "state": {"value": ["ALARM"]}},
                ),
                targets=[target],
            ),
        ):
            nag.acknowledge(
                rule, {"Serverless-EventBusDLQ": "Failed runs go to the runner's dead-letter queue; the trigger stays"}
            )

    def _programming(self, envelope: Envelope, runner: FastFunction, library_table: str) -> None:
        """The programming agent's hourly trigger, its reads of the programmed channels' schedules and of their
        stored plans (whose start the publish lead bounds)."""
        if not envelope.programmed:
            return
        runner.allow(
            ["mediatailor:GetChannelSchedule"],
            [arn(self, "mediatailor", "channel", name) for name in envelope.programmed],
        )
        _allow_plans(runner, library_table, envelope.programmed)
        self._tick(Role.PROGRAMMING, runner, "the programming agent looks at the programmed channels' runway (R6)")

    def _tick(self, role: Role, runner: FastFunction, what: str) -> None:
        rule = events.Rule(
            self,
            f"{role.value.title()}Tick",
            description=f"Agents: every hour, {what}",
            schedule=events.Schedule.cron(minute=TICKS[role]),
            targets=[events_targets.LambdaFunction(runner.function, retry_attempts=2, max_event_age=MAX_EVENT_AGE)],
        )
        nag.acknowledge(
            rule, {"Serverless-EventBusDLQ": "Failed runs go to the runner's dead-letter queue; the next hour retries"}
        )

    def _interceptor(
        self,
        envelope: Envelope,
        *,
        identity: OperatorIdentity,
        roles: Iterable[Role],
        tools: Iterable[ToolSpec[Any, Any]],
        library_table: str,
        alert_topic: sns.ITopic,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy,
    ) -> FastFunction:
        """The Gateway's request interceptor (module docstring), with the caps' writes, the ledger chains of the agents
        that are on, the programmed channels' plans and the alert topic."""
        roles, prefix = list(roles), self._prefix
        non_read = {
            spec.qualified_name: spec.approval is Approval.CONFIRM for spec in tools if spec.access is not Access.READ
        }
        function = FastFunction(
            self,
            "Interceptor",
            handler=INTERCEPTOR_HANDLER,
            description="Agents: the Gateway's check of every agent tool call against the autonomy envelope",
            environment={
                "FAST_PREFIX": prefix,
                "ENVELOPE": envelope.document,
                "TOOLS": json.dumps(non_read, sort_keys=True, separators=(",", ":")),
                **{f"CLIENT_{role.name}": identity.agent_clients[role].user_pool_client_id for role in roles},
                "AGENTS_TABLE": table_name(prefix),
                "LIBRARY_TABLE": library_table,
                "LEDGER_TABLE": ledger_table(prefix),
                "ALERT_TOPIC_ARN": alert_topic.topic_arn,
            },
            timeout=INTERCEPTOR_TIMEOUT,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        nag.acknowledge(
            function, {nag.LAMBDA_DLQ: "Invoked synchronously by the AgentCore Gateway, which refuses the call"}
        )
        function.allow(
            ["dynamodb:UpdateItem"],  # TransactWriteItems of the caps' conditional updates
            [arn(self, "dynamodb", "table", table_name(prefix))],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"CAP#{role.value}*" for role in roles]}},
        )
        for role in roles:
            _allow_ledger(function, prefix, role.chain_id)
        if envelope.programmed:
            _allow_plans(function, library_table, envelope.programmed)
        alert_topic.grant_publish(function.function)
        name = f"{prefix}-agent-interceptor-errors"
        self.alarms[name] = alarm(
            self,
            "InterceptorErrorsAlarm",
            name=name,
            metric=function.function.metric_errors(period=PERIOD, statistic=cw.Stats.SUM),
            threshold=1,
            description="The Gateway's request interceptor failed: the Gateway cannot answer the requests it fails on, "
            "operators' included; see its log group",
            topic=alert_topic,
        )
        return function

    def _alarm_on(self, prefix: str, role: Role, runner: FastFunction, queue: sqs.Queue, topic: sns.ITopic) -> None:
        name = f"{prefix}-agent-{role.value}-errors"
        self.alarms[name] = alarm(
            self,
            f"{role.value.title()}ErrorsAlarm",
            name=name,
            metric=runner.function.metric_errors(period=PERIOD, statistic=cw.Stats.SUM),
            threshold=1,
            description=f"A {role.value} agent run failed (it is retried once); see the runner's log group",
            topic=topic,
        )
        name = f"{prefix}-agent-{role.value}-dead-letters"
        self.alarms[name] = dead_letter_alarm(
            self,
            f"{role.value.title()}DeadLettersAlarm",
            queue=queue,
            name=name,
            description=f"{role.value.title()} agent runs that failed after their retry: what triggered them was "
            "not handled",
            topic=topic,
        )


def _allow_plans(function: FastFunction, library_table: str, channels: Iterable[str]) -> None:
    """Read the stored plans of ``channels`` (``CHANNEL#<channel>`` items of the media library)."""
    function.allow(
        ["dynamodb:GetItem"],
        [arn(function, "dynamodb", "table", library_table)],
        conditions={
            "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{CHANNEL_PREFIX}{name}" for name in channels]}
        },
    )


def _allow_ledger(function: FastFunction, prefix: str, chain: str) -> None:
    """Allow appending to one ledger chain (a put of a new entry, never an update)."""
    function.allow(
        ["dynamodb:PutItem"],
        [arn(function, "dynamodb", "table", ledger_table(prefix))],
        conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [chain]}},
    )
