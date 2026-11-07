"""The agent roster (``modules.agents``; M9, ADR-0023): each agent of the envelope that is on, with its runner.

- ``CapCounters``: the agents' table ``<prefix>-agents``, one item per cap and window (``CAP#...``, ``sk`` = the
  window's start) with a TTL a day after it (``functions.agents.caps``).
- One runner per agent (``fast_channels.functions.agents.runner``): it authenticates as the agent's machine client
  (``OperatorIdentity(agents=...)``), holds the agent's unattended conversations with the scheduler agent's Runtime,
  answers its approval-gated calls from the envelope and the caps, appends every decision and tool call to the
  ledger chain ``agents/<role>`` and escalates refusals to the alert topic.
- The operations agent's triggers (``functions.agents.operations``): the census's ``Census Changed`` to zero
  running channels on the stage's bus, and the probe canary's off-air alarm (runbook R12's trigger) going to
  ALARM. Its runner reads the always-on channels' states first, and starts a conversation only for a stopped one.
- The envelope's record (``fast_channels.functions.agents.envelope_record``), run on every deploy that changes the
  envelope: ``envelope.apply`` in the ledger chain ``agents/envelope``.

The Gateway's Cedar policies for the agents' principals come from the same envelope (``AgentGateway(agents=...)``).
"""

from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, RemovalPolicy, triggers
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
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.alerts import PERIOD, alarm, dead_letter_alarm
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.operator.identity import AGENT_SCOPE, OperatorIdentity
from fast_channels.infra.constructs.queues import dead_letter_queue

if TYPE_CHECKING:
    from aws_cdk import aws_logs as logs
    from aws_cdk import aws_sns as sns
    from aws_cdk import aws_sqs as sqs

RUNNER_HANDLER: Final = "fast_channels.functions.agents.runner.handler"
RECORD_HANDLER: Final = "fast_channels.functions.agents.envelope_record.handler"
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
        log_retention, removal_policy: of the functions' log groups, the queues and the table.

    Attributes:
        table: the caps' counters. runners: each agent's runner, by role. dead_letters: each runner's queue.
        record: the envelope's record function. alarms: by name.

    Raises:
        ValueError: an agent that is on has no machine client.
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
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        roles = envelope.enabled()
        if missing := [role.value for role in roles if role not in identity.agent_clients]:
            raise ValueError(f"agents {missing} need their machine clients (OperatorIdentity(agents=...))")
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
                    "LEDGER_TABLE": ledger_table(prefix),
                    "ALERT_TOPIC_ARN": alert_topic.topic_arn,
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
            runner.allow(
                ["dynamodb:UpdateItem"],
                [arn(self, "dynamodb", "table", table_name(prefix))],
                conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"CAP#{role.value}*"]}},
            )
            _allow_ledger(runner, prefix, role.chain_id)
            alert_topic.grant_publish(runner.function)
            if role is Role.OPERATIONS:
                self._operations(prefix, envelope, runner, event_bus, off_air_alarm)
            self.runners[role] = runner
            self.dead_letters[role] = queue
            self._alarm_on(prefix, role, runner, queue, alert_topic)
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


def _allow_ledger(function: FastFunction, prefix: str, chain: str) -> None:
    """Allow appending to one ledger chain (a put of a new entry, never an update)."""
    function.allow(
        ["dynamodb:PutItem"],
        [arn(function, "dynamodb", "table", ledger_table(prefix))],
        conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [chain]}},
    )
