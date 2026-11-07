"""``Readiness``: the creative registry and the state machine that takes a creative from registration to READY.

OPTIMIZATION(aws): W-2 FR-3: MediaTailor has no creative-readiness API, so this conditions each creative with the
stage's own ingest pipeline and publishes it to the own book only once it conforms (docs/creatives.md).

A Standard JSONata state machine (``<prefix>-creatives``; logs ``/aws/vendedlogs/states/<prefix>-creatives``, X-Ray),
whose input is a registration or a person's decision (``core.creatives.registry``):

- **register**: ``Received`` -> ``Fetch`` (the source into the quarantine bucket) -> ``Moderate`` (Amazon Rekognition,
  polled every 15 s) -> APPROVED: ``Conditioning`` -> ``Ingest`` (a child execution of ``<prefix>-ingest``,
  ``kind: ad``) -> ``Conform`` -> CONDITIONED: ``Publish`` (READY, ``creatives/ready.json``, the PDS publisher);
  REVIEW stops for a person, REJECTED stops;
- **approve**, **reject**, **suspend**, **resume**: ``Decide``, then conditioning (approve) or a publish (suspend,
  resume).

Every failure is recorded (``RecordFailure``: FAILED where the state allows it) before the execution fails.
"""

from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from fast_channels.core.creatives import names
from fast_channels.infra import nag
from fast_channels.infra.arns import arn, executions
from fast_channels.infra.constructs.functions import FastFunction

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

HANDLER: Final = "fast_channels.functions.creatives.readiness.handler"
MODERATION_POLL: Final = Duration.seconds(15)
SOURCE_DAYS: Final = 30
"""Days a source file stays in quarantine (its encode is what the own book serves)."""


class Readiness(Construct):
    """The registry, the quarantine bucket, the readiness function and its state machine.

    Args:
        prefix: the stage's prefix.
        ladder: the ladder creatives are conditioned to (the stage's default ladder, the own book's).
        moderation: moderate each creative with Amazon Rekognition.
        min_confidence: the label confidence (percent) that sends a creative to review.
        media_bucket: the media bucket, where the ingest pipeline writes the creatives' CDN copies.
        config_bucket: the PDS config bucket, where the READY creatives go.
        publisher: the PDS snapshot publisher's function name.
        log_retention: of the function's and the state machine's logs.
        removal_policy: of the registry and the quarantine bucket (``DESTROY`` also empties the bucket).

    Attributes:
        table, bucket, function, state_machine.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        ladder: Ladder,
        moderation: bool,
        min_confidence: float,
        media_bucket: s3.IBucket,
        config_bucket: s3.IBucket,
        publisher: str,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.table = dynamodb.TableV2(
            self,
            "Registry",
            table_name=names.table(prefix),
            partition_key=dynamodb.Attribute(name="crid", type=dynamodb.AttributeType.STRING),
            global_secondary_indexes=[
                dynamodb.GlobalSecondaryIndexPropsV2(
                    index_name=names.STATE_INDEX,
                    partition_key=dynamodb.Attribute(name="state", type=dynamodb.AttributeType.STRING),
                    sort_key=dynamodb.Attribute(name="crid", type=dynamodb.AttributeType.STRING),
                )
            ],
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=removal_policy,
        )
        self.bucket = s3.Bucket(
            self,
            "Quarantine",
            bucket_name=names.quarantine_bucket(prefix, stack.account, stack.region),
            encryption=s3.BucketEncryption.S3_MANAGED,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            removal_policy=removal_policy,
            auto_delete_objects=removal_policy == RemovalPolicy.DESTROY,
            lifecycle_rules=[
                s3.LifecycleRule(id="sources", prefix=names.INCOMING, expiration=Duration.days(SOURCE_DAYS)),
                s3.LifecycleRule(
                    id="abort-incomplete-uploads", abort_incomplete_multipart_upload_after=Duration.days(7)
                ),
            ],
        )
        nag.acknowledge(
            self.bucket,
            {
                nag.S3_SERVER_ACCESS_LOGS: "Quarantine of creative sources: written by the readiness function and the "
                "operator script, read by moderation and the ingest pipeline, each of which records the object",
            },
        )
        self.function = self._function(
            prefix=prefix,
            ladder=ladder,
            moderation=moderation,
            min_confidence=min_confidence,
            media_bucket=media_bucket,
            config_bucket=config_bucket,
            publisher=publisher,
            log_retention=log_retention,
        )
        ingest = sfn.StateMachine.from_state_machine_name(self, "Ingest", f"{prefix}-ingest")
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/vendedlogs/states/{names.state_machine(prefix)}",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_name=names.state_machine(prefix),
            comment="Register, moderate, condition, check and publish a creative (docs/creatives.md)",
            query_language=sfn.QueryLanguage.JSONATA,
            definition_body=sfn.DefinitionBody.from_chainable(self._definition(ingest)),
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=True),
            tracing_enabled=True,
            timeout=Duration.hours(2),
        )
        nag.acknowledge_references(
            self.state_machine.role,
            {
                nag.wildcard_resource(): "X-Ray has no resource-level permissions",
                nag.wildcard_resource(f"{self.function.function.function_arn}:*"): "Invoke the step and its versions",
                nag.wildcard_resource(f"{executions(ingest)}*"): "Describe and stop the creatives' ingest executions, "
                "whose names are only known at run time",
            },
        )

    def _function(
        self,
        *,
        prefix: str,
        ladder: Ladder,
        moderation: bool,
        min_confidence: float,
        media_bucket: s3.IBucket,
        config_bucket: s3.IBucket,
        publisher: str,
        log_retention: logs.RetentionDays,
    ) -> FastFunction:
        function = FastFunction(
            self,
            "Function",
            handler=HANDLER,
            description="Creatives: the readiness steps (register, copy, moderate, condition, check, publish)",
            environment={
                "CREATIVES_TABLE": self.table.table_name,
                "CREATIVES_QUARANTINE_BUCKET": self.bucket.bucket_name,
                "CREATIVES_LADDER": ladder.model_dump_json(),
                "CREATIVES_MODERATION": "true" if moderation else "false",
                "CREATIVES_MIN_CONFIDENCE": f"{min_confidence:g}",
                "MEDIA_BUCKET": media_bucket.bucket_name,
                "PDS_CONFIG_BUCKET": config_bucket.bucket_name,
                "PDS_CREATIVES_KEY": names.READY_KEY,
                "PDS_PUBLISHER_FUNCTION": publisher,
            },
            timeout=Duration.minutes(5),
            memory_size=1024,
            log_retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY,
        )
        nag.acknowledge(function, {nag.LAMBDA_DLQ: "Invoked synchronously by the readiness state machine"})
        function.allow(["dynamodb:GetItem", "dynamodb:UpdateItem"], [self.table.table_arn])
        function.allow(["dynamodb:Query"], [f"{self.table.table_arn}/index/{names.STATE_INDEX}"])
        function.allow(
            ["s3:GetObject", "s3:PutObject"],
            [self.bucket.arn_for_objects(f"{names.INCOMING}*")],
            why="Each creative's source directory, incoming/<crid>/",
        )
        function.allow(["s3:ListBucket"], [self.bucket.bucket_arn])  # a missing upload is a 404, not a 403
        function.allow(["s3:GetObject"], [media_bucket.arn_for_objects("ads/*")], why="The creatives' CDN copies")
        function.allow(["s3:PutObject"], [config_bucket.arn_for_objects(names.READY_KEY)])
        function.allow(["lambda:InvokeFunction"], [arn(self, "lambda", "function", publisher, colon=True)])
        function.allow(
            ["rekognition:StartContentModeration", "rekognition:GetContentModeration"],
            ["*"],
            why="Rekognition's video moderation has no resource-level permissions",
        )
        return function

    def _definition(self, ingest: sfn.IStateMachine) -> sfn.IChainable:
        steps = Construct(self, "Steps")
        function, payload = self.function.function, sfn.TaskInput.from_object
        execution = "{% $states.context.Execution.Name %}"

        def step(name: str, body: dict[str, object], assign: dict[str, object] | None = None) -> tasks.LambdaInvoke:
            task = tasks.LambdaInvoke.jsonata(
                steps,
                name,
                lambda_function=function,
                payload=payload(body),
                outputs="{% $states.result.Payload %}",
                assign=assign,
            )
            task.add_catch(record_failure, errors=[sfn.Errors.ALL])
            return task

        record_failure = tasks.LambdaInvoke.jsonata(
            steps,
            "RecordFailure",
            lambda_function=function,
            payload=payload(
                {
                    "step": "failed",
                    "input": "{% $states.context.Execution.Input %}",
                    "execution": execution,
                    "error": "{% $states.input %}",
                }
            ),
            outputs="{% $states.result.Payload %}",
        ).next(sfn.Fail.jsonata(steps, "Failed", error="{% $states.input.error %}", cause="{% $states.input.cause %}"))

        publish = step("Publish", {"step": "publish", "crid": "{% $crid %}", "execution": execution}).next(
            sfn.Succeed.jsonata(steps, "Ready", outputs="{% $states.input %}")
        )
        conform = step("Conform", {"step": "conform", "crid": "{% $crid %}", "asset": "{% $states.input %}"}).next(
            sfn.Choice.jsonata(steps, "Conformant")
            .when(sfn.Condition.jsonata("{% $states.input.state = 'CONDITIONED' %}"), publish)
            .otherwise(sfn.Succeed.jsonata(steps, "Rejected", outputs="{% $states.input %}"))
        )
        run = tasks.StepFunctionsStartExecution.jsonata(
            steps,
            "Ingest",
            state_machine=ingest,
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            associate_with_parent=True,
            name="{% $states.input.correlation_id %}",
            input=payload(
                {
                    "source_uri": "{% $states.input.source_uri %}",
                    "kind": "{% $states.input.kind %}",
                    "asset_id": "{% $states.input.asset_id %}",
                    "ladder_version": "{% $states.input.ladder_version %}",
                    "correlation_id": "{% $states.input.correlation_id %}",
                    "metadata": "{% $states.input.metadata %}",
                }
            ),
            outputs="{% $states.result.Output %}",
        )
        run.add_catch(record_failure, errors=[sfn.Errors.ALL])
        conditioning = step(
            "Conditioning", {"step": "conditioning", "crid": "{% $crid %}", "execution": execution}
        ).next(run.next(conform))

        moderate = step(
            "Moderate",
            {"step": "moderate", "crid": "{% $crid %}", "job": "{% $job %}"},
            assign={"job": "{% $states.result.Payload.job %}"},
        )
        wait = sfn.Wait.jsonata(steps, "WaitForModeration", time=sfn.WaitTime.duration(MODERATION_POLL))
        moderate.next(
            sfn.Choice.jsonata(steps, "Moderated")
            .when(sfn.Condition.jsonata("{% $states.input.status = 'IN_PROGRESS' %}"), wait.next(moderate))
            .when(sfn.Condition.jsonata("{% $states.input.state = 'APPROVED' %}"), conditioning)
            .otherwise(sfn.Succeed.jsonata(steps, "AwaitingReview", outputs="{% $states.input %}"))
        )
        register = step(
            "Received",
            {"step": "received", "input": "{% $states.input %}", "execution": execution},
            # A JSONata null, not None (which CDK leaves out): Moderate reads $job, which must be bound.
            assign={"crid": "{% $states.input.crid %}", "job": "{% null %}"},
        ).next(step("Fetch", {"step": "fetch", "crid": "{% $crid %}"}).next(moderate))
        decide = step(
            "Decide",
            {"step": "decide", "input": "{% $states.input %}", "execution": execution},
            assign={"crid": "{% $states.input.crid %}"},
        ).next(
            sfn.Choice.jsonata(steps, "Decided")
            .when(sfn.Condition.jsonata("{% $states.input.next = 'condition' %}"), conditioning)
            .when(sfn.Condition.jsonata("{% $states.input.next = 'publish' %}"), publish)
            .otherwise(sfn.Succeed.jsonata(steps, "Done", outputs="{% $states.input %}"))
        )
        return (
            sfn.Choice.jsonata(steps, "Action")
            .when(sfn.Condition.jsonata("{% $states.input.action = 'register' %}"), register)
            .otherwise(decide)
        )
