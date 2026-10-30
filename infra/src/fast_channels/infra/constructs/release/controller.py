"""The Release Controller (DESIGN §8): the only writer of Monetization Functions and sibling playback configurations.

- ``Custom::DesiredState`` publishes the bundle CDK rendered (validated again with its deploy-time values) to the
  release bucket and puts ``Desired State Rendered`` on the stage's bus when its digest is new.
- The rule on that event starts the state machine ``<prefix>-release`` (Step Functions Standard): prepare (validate,
  allocate the release, choose the siblings) → functions → configure each chosen sibling → publish (state, the
  playback plans, the ledger) → drift check. Any failure after prepare is ledgered as ``release.fail``.
- The drift check also runs every hour (EventBridge Scheduler) and publishes ``ReleaseDrift``; its alarm fires on
  any drift.
- ``Custom::ReleaseReaper`` deletes what the controller created when the stack is deleted.

The controller's functions are the only roles of the stage the ownership guard
(:class:`~fast_channels.infra.constructs.release.guards.ReleaseGuard`) exempts: apply and the reaper.
"""

from collections.abc import Sequence
from typing import Any, Final

from aws_cdk import ArnFormat, CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from aws_cdk import aws_sns as sns
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from fast_channels.core.demand import origin_secret
from fast_channels.core.events import DETAIL_TYPES, DesiredStateRendered, event_source
from fast_channels.core.monetization import names
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs._provider import provider
from fast_channels.infra.constructs.alerts import alarm, dead_letter_alarm
from fast_channels.infra.constructs.functions import METRICS_NAMESPACE, FastFunction
from fast_channels.infra.constructs.queues import dead_letter_queue

HANDLERS: Final = "fast_channels.functions.release"
DRIFT_INTERVAL: Final = Duration.hours(1)
APPLY_TIMEOUT: Final = Duration.minutes(90)
"""Also how long a dead execution's lock lasts (``functions.release.store.LOCK_SECONDS``)."""
BUSY_RETRY: Final = Duration.seconds(30)
BUSY_ATTEMPTS: Final = 40
"""A release waits up to 20 minutes for the one applying before it."""


class ReleaseController(Construct):
    """The bundle's publication, the apply path, the drift check and the reaper.

    Args:
        prefix: deployment prefix.
        channels: the monetized channels.
        bundle: the bundle JSON (CDK tokens resolved at deploy).
        event_bus: the stage's bus.
        topic: the stage's alert topic.
        ledger_table: name of the ledger table (``<prefix>-ledger``).
        media_bucket: name of the media bucket (playback plans).
        cdn_base_url: the CDN (playback URLs).
        log_destination_arn: the siblings' log destination (:class:`ReleaseLogs`).
        lake_deliveries: JSON of the lake's deliveries by log type (``Settings.lake_deliveries``).
        origin_secret_region: region of the demand gate's secrets.
        log_retention: of the functions' and the state machine's logs.
        removal_policy: of the state table and the bucket.

    Attributes:
        apply, drift, desired_state, reaper: the functions. state_machine: the apply path. table: the state.
        bucket: the bundles.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        channels: Sequence[str],
        bundle: str,
        event_bus: events.IEventBus,
        topic: sns.ITopic,
        ledger_table: str,
        media_bucket: str,
        cdn_base_url: str,
        log_destination_arn: str,
        lake_deliveries: str,
        origin_secret_region: str,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self._prefix, self._channels = prefix, tuple(channels)
        self.table = dynamodb.TableV2(
            self,
            "State",
            table_name=names.state_table(prefix),
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=removal_policy,
        )
        self.bucket = s3.Bucket(
            self,
            "Bundles",
            encryption=s3.BucketEncryption.S3_MANAGED,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            versioned=True,
            removal_policy=removal_policy,
            auto_delete_objects=removal_policy is RemovalPolicy.DESTROY,
        )
        nag.acknowledge(
            self.bucket,
            {"AwsSolutions-S1": "Holds only the bundles CloudFormation renders; every write is in the stack's history"},
        )
        environment = {
            "FAST_PREFIX": prefix,
            "RELEASE_BUCKET": self.bucket.bucket_name,
            "RELEASE_TABLE": self.table.table_name,
            "LEDGER_TABLE": ledger_table,
            "MEDIA_BUCKET": media_bucket,
            "CDN_BASE_URL": cdn_base_url,
            "RELEASE_LOG_DESTINATION": log_destination_arn,
            "LAKE_DELIVERIES": lake_deliveries,
            "ORIGIN_SECRET_REGION": origin_secret_region,
            "RELEASE_CHANNELS": ",".join(channels),
        }
        self.apply = self._function("Apply", "apply", "Release Controller: one step of the apply path", environment)
        self.drift = self._function(
            "Drift", "drift", "Release Controller: drift of siblings, Functions, policies", environment
        )
        self._grant(ledger_table, media_bucket, log_destination_arn, origin_secret_region)
        self.state_machine = self._state_machine()
        dead_letters = dead_letter_queue(self, "DeadLetters", removal_policy=removal_policy)
        rule = events.Rule(
            self,
            "OnDesiredState",
            description="Apply each new release bundle",
            event_bus=event_bus,
            event_pattern=events.EventPattern(
                source=[event_source(prefix)], detail_type=[DETAIL_TYPES[DesiredStateRendered]]
            ),
            targets=[
                events_targets.SfnStateMachine(
                    self.state_machine,
                    input=events.RuleTargetInput.from_object(
                        {
                            "bundle_key": events.EventField.from_path("$.detail.bundle_key"),
                            "digest": events.EventField.from_path("$.detail.digest"),
                            "correlation_id": events.EventField.from_path("$.detail.correlation_id"),
                        }
                    ),
                    retry_attempts=3,
                    dead_letter_queue=dead_letters,
                )
            ],
        )
        dead_letter_alarm(
            self,
            "DeadLettersAlarm",
            queue=dead_letters,
            name=f"{prefix}-release-dead-letters",
            description="A Desired State Rendered event could not start the Release Controller: the bundle is not "
            "applied. Start the state machine with the event's detail (bundle_key, digest, correlation_id) as input",
            topic=topic,
        )
        self._drift_schedule(topic)
        self.desired_state = self._function(
            "DesiredState", "desired_state", "Release Controller: publish the rendered bundle", {}
        )
        self.desired_state.allow(["s3:PutObject"], [self.bucket.arn_for_objects("release/*")], why="the bundles")
        event_bus.grant_put_events_to(self.desired_state.function)
        CustomResource(
            self,
            "Bundle",
            resource_type="Custom::DesiredState",
            service_token=provider(
                self, "DesiredStateProvider", on_event=self.desired_state, log_retention=log_retention
            ).service_token,
            properties={
                "Bundle": bundle,
                "Bucket": self.bucket.bucket_name,
                "Prefix": prefix,
                "EventBus": event_bus.event_bus_name,
            },
        ).node.add_dependency(self.state_machine, self.bucket, rule)  # the rule exists before the first event
        self.reaper = self._function("Reaper", "reaper", "Release Controller: delete its siblings and Functions", {})
        self._grant_reaper(media_bucket)
        reaper = CustomResource(
            self,
            "ReaperResource",
            resource_type="Custom::ReleaseReaper",
            service_token=provider(
                self, "ReaperProvider", on_event=self.reaper, log_retention=log_retention
            ).service_token,
            properties={"Prefix": prefix, "Channels": list(channels), "MediaBucket": media_bucket},
        )
        reaper.node.add_dependency(self.table, self.state_machine)  # deleted before them, while nothing can apply

    def _function(self, id: str, module: str, description: str, environment: dict[str, str]) -> FastFunction:
        function = FastFunction(
            self,
            id,
            handler=f"{HANDLERS}.{module}.handler",
            description=description,
            environment=environment,
            timeout=Duration.minutes(5),
            memory_size=512,
            log_retention=logs.RetentionDays.ONE_MONTH,
        )
        nag.acknowledge(function, {nag.LAMBDA_DLQ: "Invoked synchronously by Step Functions, Scheduler or a Provider"})
        return function

    def _mt(self, resource: str, name: str) -> str:
        return arn(self, "mediatailor", resource, name)

    def _grant(
        self,
        ledger_table: str,
        media_bucket: str,
        log_destination_arn: str,
        origin_secret_region: str,
    ) -> None:
        siblings = [self._mt("playbackConfiguration", names.sibling_pattern(c)) for c in self._channels]
        functions = self._mt("function", names.function_pattern(self._prefix))
        ledger = arn(self, "dynamodb", "table", ledger_table)
        secrets = Stack.of(self).format_arn(
            service="secretsmanager",
            region=origin_secret_region,
            resource="secret",
            resource_name=f"{origin_secret(self._prefix, '')}*",
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )
        for function in (self.apply, self.drift):
            function.allow(["dynamodb:GetItem", "dynamodb:Scan"], [self.table.table_arn])
            function.allow(["s3:GetObject"], [self.bucket.arn_for_objects("release/*")], why="the bundles")
            function.allow(["secretsmanager:GetSecretValue"], [secrets], why="origin secrets A and B of the stage")
            function.allow(["mediatailor:GetPlaybackConfiguration"], siblings, why="the monetized channels' siblings")
            function.allow(["mediatailor:GetFunction"], [functions], why="the deployment's Functions (<p>_*)")
        self.drift.allow(["mediatailor:GetChannelPolicy"], [self._mt("channel", c) for c in self._channels] or ["*"])
        apply = self.apply
        apply.allow(["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"], [self.table.table_arn])
        apply.allow(
            ["dynamodb:PutItem"],
            [ledger],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["release/*"]}},
        )
        apply.allow(
            ["mediatailor:PutFunction", "mediatailor:TagResource"],
            [functions],
            why="the deployment's Functions (<p>_*), never updated once created",
        )
        apply.allow(
            [
                "mediatailor:PutPlaybackConfiguration",
                "mediatailor:ConfigureLogsForPlaybackConfiguration",
                "mediatailor:AllowVendedLogDeliveryForResource",
                "mediatailor:TagResource",
            ],
            siblings,
            why="the monetized channels' siblings (<channel>-p-*)",
        )
        # ConfigureLogsForPlaybackConfiguration refuses a caller that may not create MediaTailor's service-linked
        # role, even when the role exists ("Cannot create the service-linked role ... add the
        # iam:CreateServiceLinkedRole permission", observed on fastm 2026-10-29).
        stack = Stack.of(self)
        apply.allow(
            ["iam:CreateServiceLinkedRole"],
            [f"arn:{stack.partition}:iam::{stack.account}:role/aws-service-role/mediatailor.amazonaws.com/*"],
            why="MediaTailor's service-linked role, which logging needs",
            conditions={"StringEquals": {"iam:AWSServiceName": "mediatailor.amazonaws.com"}},
        )
        sources = arn(self, "logs", "delivery-source", f"{self._prefix}-*", colon=True)
        deliveries = arn(self, "logs", "delivery", "*", colon=True)
        destinations = [
            log_destination_arn,
            arn(self, "logs", "delivery-destination", f"{self._prefix}-lake-*", colon=True),
        ]
        apply.allow(["logs:PutDeliverySource"], [sources], why="the siblings' delivery sources (<prefix>-*)")
        apply.allow(
            ["logs:CreateDelivery"],
            [sources, deliveries, *destinations],
            why="delivery ids are generated; the destinations are the module's log group and the lake",
        )
        apply.allow(["logs:TagResource"], [sources, deliveries], why="tags of the siblings' log deliveries")
        apply.allow(["logs:DescribeDeliveries", "logs:DescribeDeliverySources"], ["*"])
        apply.allow(
            ["s3:PutObject"], [f"arn:{Stack.of(self).partition}:s3:::{media_bucket}/plan/playback/*"], why="the plans"
        )

    def _grant_reaper(self, media_bucket: str) -> None:
        reaper = self.reaper
        reaper.allow(["mediatailor:ListPlaybackConfigurations", "mediatailor:ListFunctions"], ["*"])
        reaper.allow(
            ["mediatailor:DeletePlaybackConfiguration"],
            [self._mt("playbackConfiguration", "*-p-?")],
            why="siblings, also of channels taken out of the module; the reaper deletes only those tagged as the "
            "controller's",
        )
        reaper.allow(
            ["mediatailor:ListPrefetchSchedules"],
            [self._mt("prefetchSchedule", "*")],
            why="the siblings' prefetch schedules, deleted before the siblings; ListPrefetchSchedules authorizes on "
            "prefetchSchedule/* whatever configuration it names (seen live)",
        )
        reaper.allow(
            ["mediatailor:DeletePrefetchSchedule"],
            [self._mt("prefetchSchedule", "*-p-?/*")],
            why="every prefetch schedule of a sibling the reaper deletes",
        )
        reaper.allow(
            ["mediatailor:DeleteFunction"],
            [self._mt("function", names.function_pattern(self._prefix))],
            why="the deployment's Functions",
        )
        reaper.allow(["logs:DescribeDeliverySources", "logs:DescribeDeliveries"], ["*"])
        reaper.allow(
            ["logs:DeleteDelivery", "logs:DeleteDeliverySource"],
            [
                arn(self, "logs", "delivery", "*", colon=True),
                arn(self, "logs", "delivery-source", "*-p-?-*", colon=True),
            ],
            why="the siblings' deliveries and delivery sources (<sibling>-<log type>)",
        )
        reaper.allow(
            ["s3:DeleteObject"],
            [f"arn:{Stack.of(self).partition}:s3:::{media_bucket}/plan/playback/*"],
            why="the plans",
        )

    def _invoke(self, id: str, payload: dict[str, Any] | str, **options: Any) -> tasks.LambdaInvoke:
        return tasks.LambdaInvoke.jsonata(
            self,
            id,
            lambda_function=self.apply.function,
            payload=sfn.TaskInput.from_object(payload)
            if isinstance(payload, dict)
            else sfn.TaskInput.from_text(payload),
            outputs="{% $states.result.Payload %}",
            retry_on_service_exceptions=True,
            **options,
        )

    def _state_machine(self) -> sfn.StateMachine:
        execution = "{% $states.context.Execution.Name %}"
        prepare = self._invoke(
            "Prepare",
            {
                "step": "prepare",
                "bundle_key": "{% $states.input.bundle_key %}",
                "digest": "{% $states.input.digest %}",
                # The event's id (a start by hand without one is its own request).
                "request": "{% $exists($states.input.correlation_id) ? $states.input.correlation_id : "
                "$states.context.Execution.Name %}",
                "execution": execution,
            },
            assign={"plan": "{% $states.result.Payload %}"},
        )
        context = {
            "bundle_key": "{% $plan.bundle_key %}",
            "digest": "{% $plan.digest %}",
            "release": "{% $plan.release %}",
            "change_id": "{% $plan.change_id %}",
            "execution": execution,
        }
        accept = self._invoke(
            "Accept",
            {"step": "accept", "request": "{% $plan.request %}", **context},
            assign={"plan": "{% $merge([$plan, $states.result.Payload]) %}"},
        )
        accept.add_retry(  # another release holds the lock: wait for it
            errors=["ReleaseBusyError"], interval=BUSY_RETRY, max_attempts=BUSY_ATTEMPTS, backoff_rate=1
        )
        functions = self._invoke("Functions", {"step": "functions", "siblings": "{% $plan.siblings %}", **context})
        configure = sfn.Map.jsonata(
            self,
            "Configure",
            items=sfn.ProvideItems.jsonata("{% $plan.siblings %}"),
            max_concurrency=1,  # deliveries to one destination are created one at a time
            item_selector={"step": "configure", "sibling": "{% $states.context.Map.Item.Value %}", **context},
        ).item_processor(self._invoke("ConfigureSibling", "{% $states.input %}"))
        publish = self._invoke("Publish", {"step": "publish", "results": "{% $states.input %}", **context})
        publish.add_retry(errors=["StaleStateError"], max_attempts=3)  # another writer changed the channel first
        drift = tasks.LambdaInvoke.jsonata(
            self,
            "CheckDrift",
            lambda_function=self.drift.function,
            payload=sfn.TaskInput.from_object({"change_id": "{% $plan.change_id %}"}),
            outputs="{% $states.result.Payload %}",
            retry_on_service_exceptions=True,
        )
        failed = self._invoke(
            "RecordFailure",
            {
                "step": "fail",
                "release": "{% $exists($plan.release) ? $plan.release : null %}",
                "change_id": "{% $exists($plan.change_id) ? $plan.change_id : null %}",
                "error": "{% $states.input %}",
                "execution": execution,
            },
        ).next(sfn.Fail(self, "ReleaseFailed", error="ReleaseFailed", cause="See the ledger's release.fail record"))
        rejected = sfn.Fail(
            self, "BundleRejected", error="BundleRejected", cause="See the ledger's release.reject record"
        )
        prepare.add_catch(rejected, errors=["InvalidBundleError"], outputs="{% $states.errorOutput %}")
        for step in (prepare, accept, functions, configure, publish):
            step.add_catch(failed, outputs="{% $states.errorOutput %}")
        skip = sfn.Choice(self, "AlreadyReleased?").when(
            sfn.Condition.jsonata("{% $plan.skip %}"), sfn.Succeed(self, "NothingToDo")
        )
        definition = prepare.next(skip.otherwise(accept.next(functions).next(configure).next(publish).next(drift)))
        state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_name=names.state_machine(self._prefix),
            comment="Release Controller: validate the bundle, take the lock, create its Functions, configure the idle "
            "siblings, publish the plans and ledger the release (DESIGN §8.3)",
            query_language=sfn.QueryLanguage.JSONATA,
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=APPLY_TIMEOUT,
        )
        nag.acknowledge_references(
            state_machine.role,
            {
                nag.wildcard_resource(): "X-Ray has no resource-level permissions",
                nag.wildcard_resource(f"{self.apply.function.function_arn}:*"): "Invoke the steps and their versions",
                nag.wildcard_resource(f"{self.drift.function.function_arn}:*"): "Invoke the drift check's versions",
            },
        )
        return state_machine

    def _drift_schedule(self, topic: sns.ITopic) -> None:
        role = iam.Role(
            self,
            "DriftScheduleRole",
            description="EventBridge Scheduler: run the release drift check",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": Stack.of(self).account}}
            ),
        )
        nag.acknowledge_references(
            role,
            {
                nag.wildcard_resource(
                    f"{self.drift.function.function_arn}:*"
                ): "Scheduler L2 grant: the function's versions"
            },
        )
        scheduler.Schedule(
            self,
            "DriftSchedule",
            description=f"Release drift check of {self._prefix} every hour",
            schedule=scheduler.ScheduleExpression.rate(DRIFT_INTERVAL),
            target=scheduler_targets.LambdaInvoke(
                self.drift.function,
                input=scheduler.ScheduleTargetInput.from_object({"scheduled_time": "<aws.scheduler.scheduled-time>"}),
                retry_attempts=1,
                role=role,
            ),
        )
        self.drift_alarm = alarm(
            self,
            "DriftAlarm",
            name=f"{self._prefix}-release-drift",
            metric=cw.Metric(
                namespace=METRICS_NAMESPACE,
                metric_name="ReleaseDrift",
                dimensions_map={"service": "release.drift"},
                statistic=cw.Stats.MAXIMUM,
                period=DRIFT_INTERVAL,
            ),
            threshold=1,
            description="A sibling, its Functions or a monetized channel's policy is not what the Release Controller "
            "applied (see the drift check's log for the paths)",
            topic=topic,
        )
