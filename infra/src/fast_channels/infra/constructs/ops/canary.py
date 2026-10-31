"""The ops module's probe canary: one CloudWatch Synthetics canary every five minutes over the stage's running
channels (``functions/canary/python/fast_probe.py``), started and stopped by the channel census.

A canary per channel per minute would cost about a BASIC channel's price (D§18.9), so there is one, at five minutes,
and only while a channel runs: on ``Census Changed`` a small state machine starts it (``running`` > 0) or stops it.
Its alarm ``<prefix>-probe-off-air`` (two failed runs in a row, missing data not breaching) starts runbook R12.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from aws_cdk import aws_synthetics as synthetics
from constructs import Construct

from fast_channels.core.events import DETAIL_TYPES, CensusChanged, event_source
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.alerts import alarm

CANARY_DIR: Final = Path(__file__).resolve().parents[6] / "functions" / "canary"
"""The canary's code (``python/fast_probe.py``), as Synthetics' Python runtime expects it."""
PROBE_INTERVAL: Final = Duration.minutes(5)
OFF_AIR_RUNS: Final = 2
"""Failed runs in a row that raise the alarm (R12: a probe stall on 2 consecutive polls)."""


def off_air_alarm_name(prefix: str) -> str:
    """The canary's alarm, which starts runbook R12."""
    return f"{prefix}-probe-off-air"


def canary_name(prefix: str) -> str:
    """``<prefix>-probe`` (Synthetics allows lower-case letters, digits, ``-`` and ``_``, at most 21 characters)."""
    return f"{prefix}-probe"[:21]


class ProbeCanary(Construct):
    """The canary, its census-driven start and stop, and its alarm.

    Args:
        prefix: deployment prefix.
        channels: viewer (SSAI) multivariant playlist URL of each IaC channel.
        census_parameter: the census's last-count parameter.
        event_bus: the stage's bus (``Census Changed``).
        artifacts_bucket: where Synthetics writes run artifacts (under ``synthetics/<prefix>/``).
        topic: the stage's alert topic.

    Attributes:
        canary: the canary. alarm: ``<prefix>-probe-off-air``. switch: the start-and-stop state machine.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        channels: Mapping[str, str],
        census_parameter: str,
        event_bus: events.IEventBus,
        artifacts_bucket: s3.IBucket,
        topic: sns.ITopic,
    ) -> None:
        super().__init__(scope, id)
        name = canary_name(prefix)
        self.canary = synthetics.Canary(
            self,
            "Canary",
            canary_name=name,
            runtime=synthetics.Runtime.SYNTHETICS_PYTHON_SELENIUM_7_0,
            test=synthetics.Test.custom(code=synthetics.Code.from_asset(str(CANARY_DIR)), handler="fast_probe.handler"),
            schedule=synthetics.Schedule.rate(PROBE_INTERVAL),
            start_after_creation=False,
            timeout=Duration.minutes(1),
            environment_variables={
                "CENSUS_PARAMETER": census_parameter,
                "CHANNELS": Stack.of(self).to_json_string(dict(channels)),
            },
            artifacts_bucket_location=synthetics.ArtifactsBucketLocation(
                bucket=artifacts_bucket, prefix=f"synthetics/{prefix}"
            ),
            provisioned_resource_cleanup=True,
            success_retention_period=Duration.days(7),
            failure_retention_period=Duration.days(14),
        )
        self.canary.role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"], resources=[arn(self, "ssm", "parameter", census_parameter.lstrip("/"))]
            )
        )
        self.canary.role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["mediatailor:DescribeChannel"], resources=[arn(self, "mediatailor", "channel", f"{prefix}-*")]
            )
        )
        nag.acknowledge_references(
            self.canary.role,
            {
                nag.wildcard_resource(): "Synthetics L2: CloudWatch metrics and X-Ray have no resource-level "
                "permissions",
                nag.wildcard_resource(f"{artifacts_bucket.bucket_arn}/synthetics/{prefix}/*"): "Synthetics L2: run "
                "artifacts under the stage's prefix",
                nag.wildcard_resource(arn(self, "logs", "log-group", "/aws/lambda/cwsyn-*", colon=True)): "Synthetics "
                "L2: the canary function's logs, named by Synthetics",
                nag.wildcard_resource(arn(self, "mediatailor", "channel", f"{prefix}-*")): "States of this "
                "deployment's channels (named <prefix>-*)",
                "Serverless-LambdaStarPermissions": "Synthetics L2's canary role (the wildcards above)",
            },
        )
        self.alarm = alarm(
            self,
            "OffAir",
            name=off_air_alarm_name(prefix),
            metric=self.canary.metric_success_percent(period=PROBE_INTERVAL, statistic=cw.Stats.MINIMUM),
            threshold=100,
            comparison=cw.ComparisonOperator.LESS_THAN_THRESHOLD,
            evaluation_periods=OFF_AIR_RUNS,
            description=f"The probe failed {OFF_AIR_RUNS} runs in a row on a running {prefix} channel (runbook R12)",
            topic=topic,
        )
        self.switch = self._switch(prefix, name)
        census = events.Rule(
            self,
            "OnCensus",
            description=f"Start the probe while a {prefix} channel runs, stop it when none does",
            event_bus=event_bus,
            event_pattern=events.EventPattern(source=[event_source(prefix)], detail_type=[DETAIL_TYPES[CensusChanged]]),
            targets=[events_targets.SfnStateMachine(self.switch, retry_attempts=4)],
        )
        nag.acknowledge(
            census, {"Serverless-EventBusDLQ": "EventBridge retries the switch; the next Census Changed repeats it"}
        )

    def _switch(self, prefix: str, name: str) -> sfn.StateMachine:
        steps = Construct(self, "Steps")
        canary = arn(self, "synthetics", "canary", name, colon=True)
        done = sfn.Succeed.jsonata(steps, "Done")
        start = tasks.CallAwsService.jsonata(
            steps,
            "StartCanary",
            service="synthetics",
            action="startCanary",
            parameters={"Name": name},
            iam_resources=[canary],
        ).add_catch(done, errors=["Synthetics.ConflictException"])
        stop = tasks.CallAwsService.jsonata(
            steps,
            "StopCanary",
            service="synthetics",
            action="stopCanary",
            parameters={"Name": name},
            iam_resources=[canary],
        ).add_catch(done, errors=["Synthetics.ConflictException"])
        choice = (
            sfn.Choice.jsonata(steps, "Running?")
            .when(sfn.Condition.jsonata("{% $states.input.detail.running > 0 %}"), start.next(done))
            .otherwise(stop.next(done))
        )
        state_machine = sfn.StateMachine(
            self,
            "Switch",
            state_machine_name=f"{prefix}-probe-switch",
            comment="Start the probe canary while a channel runs, stop it when none does (Census Changed)",
            query_language=sfn.QueryLanguage.JSONATA,
            definition_body=sfn.DefinitionBody.from_chainable(choice),
            timeout=Duration.minutes(2),
        )
        nag.acknowledge_references(
            state_machine.role, {nag.wildcard_resource(): "X-Ray has no resource-level permissions"}
        )
        return state_machine
