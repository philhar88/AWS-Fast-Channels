"""Origin-secret rotation (DESIGN §8.6): the two-value overlap as a state machine, every
``modules.release.secret_rotation_days`` days (a daily EventBridge Scheduler check) and by hand on a suspected
exposure.

``<prefix>-release-rotation`` (Step Functions Standard) runs ``fast_channels.functions.release.rotate`` step by step:
begin (a scheduled run that is not due ends here; the Release Controller's lock, held by this execution, retried
while a release or another rotation holds it) → admit (a new value for the other slot, at
the WAF beside the current one) → the WAF's propagation → probe (retried until the new value is admitted) → switch
(every configured sibling, and the slot) → one break → verify (retried until a break was answered; while a monetized
channel is on air) → retire (a new value for the old slot, which the WAF admits instead) → done. A failure is
ledgered (``rotation.fail``), gives the lock back and raises ``<prefix>-release-rotation-failed``; both values a
sibling may send stay admitted.
"""

from typing import TYPE_CHECKING, Any, Final

from aws_cdk import ArnFormat, Duration, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_iam as iam
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sns as sns
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from fast_channels.core.demand import origin_rule_group, origin_secret
from fast_channels.core.monetization import names
from fast_channels.infra import nag
from fast_channels.infra.constructs.alerts import alarm

if TYPE_CHECKING:
    from fast_channels.infra.constructs.functions import FastFunction

PROPAGATION: Final = Duration.minutes(1)
"""A WAF change reaches CloudFront within about a minute; the probe retries until it has."""
ONE_BREAK: Final = Duration.minutes(2)
TIMEOUT: Final = Duration.hours(2)
"""The state machine's: the wait for a release's lock is most of it."""


class ReleaseRotation(Construct):
    """The rotation function, its state machine and schedule, and the failure alarm.

    Args:
        prefix: deployment prefix.
        function: the rotation steps (``functions.release.rotate``), built by the controller with its environment.
        origin_secret_region: where the origin secrets and the CLOUDFRONT-scope rule group are (us-east-1).
        rotation_days: ``modules.release.secret_rotation_days``.
        topic: the stage's alert topic.

    Attributes:
        state_machine: ``<prefix>-release-rotation``. alarm: its failure alarm.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        function: FastFunction,
        origin_secret_region: str,
        rotation_days: int,
        topic: sns.ITopic,
    ) -> None:
        super().__init__(scope, id)
        self._function = function
        stack = Stack.of(self)
        secrets = stack.format_arn(
            service="secretsmanager",
            region=origin_secret_region,
            resource="secret",
            resource_name=f"{origin_secret(prefix, '')}*",
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )
        function.function.add_environment("SECRET_ROTATION_DAYS", str(rotation_days))
        function.allow(
            ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"],
            [secrets],
            why="origin secrets A and B of the stage",
        )
        function.allow(["secretsmanager:GetRandomPassword"], ["*"])
        function.allow(["wafv2:ListRuleGroups"], ["*"])
        function.allow(
            ["wafv2:GetRuleGroup", "wafv2:UpdateRuleGroup"],
            [
                stack.format_arn(
                    service="wafv2",
                    region=origin_secret_region,
                    resource="global/rulegroup",
                    resource_name=f"{origin_rule_group(prefix)}/*",
                    arn_format=ArnFormat.SLASH_RESOURCE_NAME,
                )
            ],
            why="the stage's origin rule group (its id is generated)",
        )
        self.state_machine = self._state_machine(prefix)
        self.alarm = alarm(
            self,
            "Failed",
            name=f"{prefix}-release-rotation-failed",
            metric=self.state_machine.metric_failed(period=Duration.minutes(5), statistic=cw.Stats.SUM),
            threshold=1,
            description="An origin-secret rotation failed (the ledger's rotation.fail record); both values a sibling "
            "may send are still admitted, and the rotation can run again",
            topic=topic,
        )
        self._schedule(prefix, rotation_days)

    def _invoke(self, id: str, payload: dict[str, Any], **options: Any) -> tasks.LambdaInvoke:
        return tasks.LambdaInvoke.jsonata(
            self,
            id,
            lambda_function=self._function.function,
            payload=sfn.TaskInput.from_object(payload),
            outputs=options.pop("outputs", "{% $states.result.Payload %}"),
            retry_on_service_exceptions=True,
            **options,
        )

    def _state_machine(self, prefix: str) -> sfn.StateMachine:
        change = {"change_id": "{% $states.context.Execution.Name %}"}
        begin = self._invoke(
            "Begin",
            {
                "step": "begin",
                "reason": "{% $exists($states.input.reason) ? $states.input.reason : 'manual' %}",
                **change,
            },
            assign={"slots": "{% $states.result.Payload %}"},
        )
        begin.add_retry(errors=["ReleaseBusyError"], interval=Duration.minutes(1), max_attempts=100, backoff_rate=1)
        admit = self._invoke("Admit", {"step": "admit", "target": "{% $slots.target %}", **change})
        admit.add_retry(errors=["WAFOptimisticLockException"], interval=Duration.seconds(5), max_attempts=5)
        probe = self._invoke("Probe", {"step": "probe", "target": "{% $slots.target %}", **change})
        probe.add_retry(errors=["NotAdmittedError"], interval=Duration.seconds(30), max_attempts=20, backoff_rate=1)
        switch = self._invoke(
            "Switch",
            {"step": "switch", "target": "{% $slots.target %}", **change},
            assign={"switched": "{% $states.result.Payload %}"},
        )
        verify = self._invoke(
            "Verify",
            {"step": "verify", "siblings": "{% $switched.switched %}", "since": "{% $switched.at %}", **change},
        )
        verify.add_retry(errors=["NotVerifiedError"], interval=Duration.minutes(1), max_attempts=15, backoff_rate=1)
        retire = self._invoke("Retire", {"step": "retire", "current": "{% $slots.current %}", **change})
        retire.add_retry(errors=["WAFOptimisticLockException"], interval=Duration.seconds(5), max_attempts=5)
        failed = self._invoke("RecordFailure", {"step": "fail", "error": "{% $states.input %}", **change}).next(
            sfn.Fail(self, "RotationFailed", error="RotationFailed", cause="See the ledger's rotation.fail record")
        )
        for step in (begin, admit, probe, switch, verify, retire):
            step.add_catch(failed, outputs="{% $states.errorOutput %}")
        rotate = (
            admit.next(sfn.Wait.jsonata(self, "Propagation", time=sfn.WaitTime.duration(PROPAGATION)))
            .next(probe)
            .next(switch)
            .next(sfn.Wait.jsonata(self, "OneBreak", time=sfn.WaitTime.duration(ONE_BREAK)))
            .next(verify)
            .next(retire)
            .next(sfn.Succeed(self, "Rotated"))
        )
        due = sfn.Choice(self, "Due?").when(sfn.Condition.jsonata("{% $slots.due %}"), rotate)
        definition = begin.next(due.otherwise(sfn.Succeed(self, "NotDue")))
        machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_name=names.rotation_state_machine(prefix),
            comment="Origin-secret rotation with two-value overlap (DESIGN §8.6)",
            query_language=sfn.QueryLanguage.JSONATA,
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=TIMEOUT,
        )
        nag.acknowledge_references(
            machine.role,
            {
                nag.wildcard_resource(): "X-Ray has no resource-level permissions",
                nag.wildcard_resource(f"{self._function.function.function_arn}:*"): "Invoke the steps' versions",
            },
        )
        return machine

    def _schedule(self, prefix: str, days: int) -> None:
        role = iam.Role(
            self,
            "ScheduleRole",
            description="EventBridge Scheduler: start the origin-secret rotation",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": Stack.of(self).account}}
            ),
        )
        scheduler.Schedule(
            self,
            "Schedule",
            # A daily check, not rate(<days>): a rate schedule starts when it is created unless it has a StartDate,
            # so every deploy would rotate. The check rotates once <days> have passed since the last rotation.
            description=f"Origin-secret rotation of {prefix}: a daily check, rotating every {days} days",
            schedule=scheduler.ScheduleExpression.rate(Duration.days(1)),
            target=scheduler_targets.StepFunctionsStartExecution(
                self.state_machine,
                input=scheduler.ScheduleTargetInput.from_object({"reason": "scheduled"}),
                retry_attempts=1,
                role=role,
            ),
        )
