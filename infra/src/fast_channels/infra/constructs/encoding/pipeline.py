"""The ingest pipeline: MediaConvert queue and role, and the state machine that encodes and registers assets
(SPEC §5.1)."""

from collections.abc import Sequence
from typing import Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_mediaconvert as mediaconvert
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from fast_channels.core.events import DETAIL_TYPES, event_source
from fast_channels.core.ingest import IngestRequest
from fast_channels.core.tags import Owner, resource_tags
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.queues import dead_letter_queue
from fast_channels.infra.grants import allow

VIDEO_EXTENSIONS: Final = (".mp4", ".m4v", ".mov", ".mxf", ".mkv", ".webm", ".avi", ".ts", ".m2ts", ".mts", ".mpg")
"""Uploads that start an ingest (MediaConvert input containers), matched case-insensitively."""
EXECUTION_TIMEOUT: Final = Duration.hours(24)
STEP_TIMEOUT: Final = Duration.minutes(1)
TRANSCODE_TIMEOUT: Final = Duration.hours(12)
EVENT_MAX_AGE: Final = Duration.hours(6)
FUNCTIONS: Final = "fast_channels.functions.ingest"
"""Package of the pipeline's and the kit's step handlers."""
STEP_FUNCTIONS_INVOKED: Final = "Invoked synchronously by Step Functions, which retries and catches its failures"


class IngestPipeline(Construct):
    """Encodes assets with a ladder and registers them with Channel Assembly (SPEC §5.1).

    A Standard state machine (JSONata; ALL-level logs with execution data to
    ``/aws/vendedlogs/states/<prefix>-ingest``; X-Ray) takes an IngestRequest
    (``contracts/ingest-request.schema.json``) and runs ``Prepare`` (probe, refusals, sparse job overrides, item
    INGESTING) -> ``Transcode`` (MediaConvert CreateJob with the run-a-job integration; Role and Queue are
    literals, the job template and overrides come from Prepare) -> ``Finalize`` (verify outputs, nominal
    BANDWIDTH, break offsets; the SGAI copy of ads) -> ``Register`` (a Map with MaxConcurrency 1 creating the VOD
    source; empty for ads) -> ``Publish`` (item READY, ``Asset Ready``). Every step catches into
    ``PublishFailure`` (item FAILED, ``Asset Failed``) and a Fail state; an execution that finds the same encode
    already in progress stops at ``AlreadyIngesting`` without touching the item.

    Uploads with a video extension to the ingest bucket and ``Ingest Requested`` events on ``event_bus`` invoke a
    trigger function (retries, then :attr:`dead_letter_queue`) that starts executions named after the request's
    correlation id. Tools and the ladder provisioner call StartExecution with ``name=correlation_id`` themselves.
    Requests that reach the dead-letter queue never started an execution: alarm on it
    (``MediaObservability(dead_letter_queues=...)``).

    Args:
        prefix: deployment prefix (names, ``fast:app``, MediaConvert ``UserMetadata.app``, event source).
        ingest_bucket: mezzanines, their sidecars and the kits' brand stills; MediaConvert and Probe read it.
        media_bucket: encoder outputs (``assets/``) and ad manifests (``ads/``).
        library: the media library table.
        event_bus: the ``fast-channels`` bus (``Asset Ready``/``Asset Failed`` out, ``Ingest Requested`` in).
        source_location_name: Channel Assembly S3 source location of the media bucket.
        content_base_url: CDN-neutral base URL of segments (absolute ad playlists, creative ids, VAST).
        package_profiles: rendition profiles registered as package configurations on every VOD source.
        default_ladder_version: ladder of requests that name none (uploads without a sidecar).
        log_retention: retention of every log group.
        removal_policy: removal policy of the log groups and the dead-letter queue.
        concurrent_jobs: concurrent MediaConvert jobs of the pipeline's on-demand queue.

    Attributes:
        state_machine: the ingest state machine (MediaExports ``ingest_state_machine``).
        queue_arn: the MediaConvert queue ARN (MediaExports ``mediaconvert_queue_arn``).
        role_arn: the MediaConvert role ARN.
        role: the MediaConvert role (reads the ingest bucket, writes ``assets/`` of the media bucket).
        queue: the ``CfnQueue``.
        dead_letter_queue: events that could not start an ingest.
        log_group: the state machine's log group.
        trigger, prepare, finalize, register, publish: the step functions (``FastFunction``).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        ingest_bucket: s3.IBucket,
        media_bucket: s3.IBucket,
        library: dynamodb.ITableV2,
        event_bus: events.IEventBus,
        source_location_name: str,
        content_base_url: str,
        package_profiles: Sequence[str],
        default_ladder_version: str,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
        concurrent_jobs: int = 20,
    ) -> None:
        super().__init__(scope, id)
        self.prefix = prefix
        self.ingest_bucket = ingest_bucket
        self.media_bucket = media_bucket
        self.library = library
        self.event_bus = event_bus
        self.source_location_name = source_location_name
        self.content_base_url = content_base_url
        self.package_profiles = tuple(package_profiles)
        self.log_retention = log_retention
        self.removal_policy = removal_policy

        self.queue = mediaconvert.CfnQueue(
            self,
            "Queue",
            name=f"{prefix}-ingest",
            description=f"FAST Channels ({prefix}): ingest and ladder-kit encodes",
            pricing_plan="ON_DEMAND",
            concurrent_jobs=concurrent_jobs,
            status="ACTIVE",
            tags=resource_tags(prefix, Owner.IAC),
        )
        self.queue_arn: str = self.queue.attr_arn
        self.role = self._mediaconvert_role()
        self.role_arn = self.role.role_arn

        self.prepare = self._step(
            "Prepare",
            "Ingest: probe, check and turn an asset into a MediaConvert job",
            {"FAST_PREFIX": prefix, "LIBRARY_TABLE": library.table_name, "MEDIA_BUCKET": media_bucket.bucket_name},
        )
        self.finalize = self._step(
            "Finalize",
            "Ingest: verify and fix up an encode; SGAI-ready ad manifests",
            {
                "LIBRARY_TABLE": library.table_name,
                "MEDIA_BUCKET": media_bucket.bucket_name,
                "CONTENT_BASE_URL": content_base_url,
                "PACKAGE_PROFILES": ",".join(package_profiles),
            },
            timeout=Duration.minutes(5),
            memory_size=1024,
        )
        self.register = self._step(
            "Register",
            "Ingest: create or refresh the Channel Assembly VOD source of an asset",
            {"FAST_PREFIX": prefix, "SOURCE_LOCATION": source_location_name},
            timeout=Duration.minutes(3),
        )
        self.publish = self._step(
            "Publish",
            "Ingest: mark an asset READY or FAILED and publish the event",
            {"FAST_PREFIX": prefix, "LIBRARY_TABLE": library.table_name, "EVENT_BUS": event_bus.event_bus_name},
        )
        self._grant_steps()

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/vendedlogs/states/{prefix}-ingest",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_name=f"{prefix}-ingest",
            comment="Encode an asset with a ladder and register it with Channel Assembly",
            query_language=sfn.QueryLanguage.JSONATA,
            definition_body=sfn.DefinitionBody.from_chainable(self._definition()),
            logs=sfn.LogOptions(destination=self.log_group, level=sfn.LogLevel.ALL, include_execution_data=True),
            tracing_enabled=True,
            timeout=EXECUTION_TIMEOUT,
        )
        nag.acknowledge_references(
            self.state_machine.role,
            {
                nag.wildcard_resource(): "MediaConvert CreateJob/GetJob/CancelJob (job ARNs exist only after "
                "creation) and X-Ray, which has no resource-level permissions",
                **{
                    nag.wildcard_resource(f"{step.function.function_arn}:*"): "Invoke the step and its versions"
                    for step in (self.prepare, self.finalize, self.register, self.publish)
                },
            },
        )

        self.dead_letter_queue = dead_letter_queue(self, "DeadLetters", removal_policy=removal_policy)
        self.trigger = self._trigger(default_ladder_version)

    def _step(
        self,
        id: str,
        description: str,
        environment: dict[str, str],
        *,
        timeout: Duration = STEP_TIMEOUT,
        memory_size: int = 512,
    ) -> FastFunction:
        step = FastFunction(
            self,
            id,
            handler=f"{FUNCTIONS}.{id.lower()}.handler",
            description=description,
            environment=environment,
            timeout=timeout,
            memory_size=memory_size,
            log_retention=self.log_retention,
            removal_policy=self.removal_policy,
        )
        nag.acknowledge(step, {nag.LAMBDA_DLQ: STEP_FUNCTIONS_INVOKED})
        return step

    def _mediaconvert_role(self) -> iam.Role:
        role = iam.Role(
            self,
            "MediaConvertRole",
            description=f"FAST Channels ({self.prefix}): MediaConvert reads mezzanines and writes encodes",
            # Confused-deputy protection: only jobs on this pipeline's queue may assume the role. MediaConvert
            # presents the job's queue ARN as aws:SourceArn (verified: a jobs/* or jobTemplates/* condition fails
            # every job, reported as "video_description [1]: Invalid frame rate: 0/0").
            assumed_by=iam.ServicePrincipal(
                "mediaconvert.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Stack.of(self).account},
                    "ArnEquals": {"aws:SourceArn": self.queue_arn},
                },
            ),
        )
        allow(
            role,
            ["s3:GetObject"],
            [self.ingest_bucket.arn_for_objects("*")],
            why="Mezzanines and the kits' brand stills may have any key",
        )
        allow(
            role,
            ["s3:PutObject"],
            [self.media_bucket.arn_for_objects("assets/*")],
            why="Encodes go to assets/<asset>/<ladder>/",
        )
        return role

    def _grant_steps(self) -> None:
        prepare, finalize, register, publish = self.prepare, self.finalize, self.register, self.publish
        library, assets = [self.library.table_arn], [self.media_bucket.arn_for_objects("assets/*")]

        prepare.allow(["mediaconvert:Probe"], ["*"], why="mediaconvert:Probe has no resource-level permissions")
        # Probe reads the source with the caller's credentials (mediaconvert.md §7.3).
        prepare.allow(["s3:GetObject"], [self.ingest_bucket.arn_for_objects("*")], why="Mezzanines of any key")
        prepare.allow(["dynamodb:GetItem", "dynamodb:PutItem"], library)
        # Whether the execution holding an INGESTING item still runs (a literal ARN: the machine needs this role).
        executions = arn(self, "states", "execution", f"{self.prefix}-ingest:*", colon=True)
        prepare.allow(["states:DescribeExecution"], [executions], why="Executions of the ingest state machine")

        finalize.allow(["s3:GetObject", "s3:PutObject"], assets, why="Manifests of any encode")
        finalize.allow(["s3:PutObject"], [self.media_bucket.arn_for_objects("ads/*")], why="SGAI-ready ad manifests")
        # ListBucket turns a missing manifest into a 404 instead of a 403.
        finalize.allow(
            ["s3:ListBucket"], [self.media_bucket.bucket_arn], conditions={"StringLike": {"s3:prefix": ["assets/*"]}}
        )
        finalize.allow(["dynamodb:UpdateItem"], library)

        register.allow(
            [
                "mediatailor:DescribeVodSource",
                "mediatailor:CreateVodSource",
                "mediatailor:UpdateVodSource",
                "mediatailor:TagResource",
            ],
            [arn(self, "mediatailor", "vodSource", f"{self.source_location_name}/*")],
            why="VOD sources of the deployment's source location",
        )
        # ListAlerts is authorized on the account's alerts resource, not on the resource queried (observed).
        register.allow(
            ["mediatailor:ListAlerts"],
            [arn(self, "mediatailor", "alerts", "*")],
            why="ListAlerts is authorized on alerts/* only",
        )
        # CreateVodSource checks that the caller itself can read the manifests (channel-assembly.md §2.3).
        register.allow(["s3:GetObject"], assets, why="Manifests of any encode")

        publish.allow(["dynamodb:UpdateItem"], library)
        self.event_bus.grant_put_events_to(publish.function)

    def _definition(self) -> sfn.IChainable:
        steps = Construct(self, "Steps")
        payload = sfn.TaskInput.from_object
        failed = tasks.LambdaInvoke.jsonata(
            steps,
            "PublishFailure",
            lambda_function=self.publish.function,
            payload=payload(
                {
                    "status": "FAILED",
                    "request": "{% $states.context.Execution.Input %}",
                    "execution": "{% $states.context.Execution.Name %}",
                    "error": "{% $states.input %}",
                }
            ),
            outputs="{% $states.result.Payload %}",
        ).next(
            sfn.Fail.jsonata(
                steps, "IngestFailed", error="{% $states.input.error %}", cause="{% $states.input.cause %}"
            )
        )
        duplicate = sfn.Fail.jsonata(
            steps, "AlreadyIngesting", error="IngestInProgressError", cause="{% $states.input.Cause %}"
        )

        prepare = tasks.LambdaInvoke.jsonata(
            steps,
            "Prepare",
            lambda_function=self.prepare.function,
            payload=payload(
                {
                    "request": "{% $states.input %}",
                    "execution": "{% $states.context.Execution.Name %}",
                    "execution_arn": "{% $states.context.Execution.Id %}",
                }
            ),
            assign={"asset": "{% $states.result.Payload %}"},
        )
        transcode = tasks.MediaConvertCreateJob.jsonata(
            steps,
            "Transcode",
            create_job_request={
                "Role": self.role_arn,
                "Queue": self.queue_arn,
                "JobTemplate": "{% $asset.job.JobTemplate %}",
                "Settings": "{% $asset.job.Settings %}",
                "UserMetadata": "{% $asset.job.UserMetadata %}",
                "ClientRequestToken": "{% $asset.job.ClientRequestToken %}",
            },
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            assign={"job_id": "{% $states.result.Job.Id %}"},
            task_timeout=sfn.Timeout.duration(TRANSCODE_TIMEOUT),
        )
        # CreateJob allows 5 TPS (burst 100); the retryable errors of the integration (mediaconvert.md §7.4).
        transcode.add_retry(
            errors=["MediaConvert.TooManyRequestsException", "MediaConvert.InternalServerErrorException"],
            interval=Duration.seconds(5),
            max_attempts=4,
            backoff_rate=2,
            jitter_strategy=sfn.JitterType.FULL,
        )
        finalize = tasks.LambdaInvoke.jsonata(
            steps,
            "Finalize",
            lambda_function=self.finalize.function,
            payload=payload({"asset": "{% $asset %}", "job_id": "{% $job_id %}"}),
            assign={"encoded": "{% $states.result.Payload %}"},
        )
        register_vod_source = tasks.LambdaInvoke.jsonata(
            steps,
            "RegisterVodSource",
            lambda_function=self.register.function,
            payload=sfn.TaskInput.from_text("{% $states.input %}"),
            outputs="{% $states.result.Payload %}",
        )
        # Throttling, or the lock Channel Assembly keeps on a VOD source still used by programs (6-8 min).
        register_vod_source.add_retry(
            errors=["VodSourceBusyError"],
            interval=Duration.seconds(15),
            max_attempts=8,
            backoff_rate=1.5,
            max_delay=Duration.minutes(2),
            jitter_strategy=sfn.JitterType.FULL,
        )
        register = sfn.Map.jsonata(
            steps,
            "Register",
            items=sfn.ProvideItems.jsonata("{% $encoded.registrations %}"),
            max_concurrency=1,  # Channel Assembly VOD-source APIs allow 1 TPS (channel-assembly.md §5.6)
            assign={"registered": "{% $states.result %}"},
        )
        register.item_processor(register_vod_source)
        publish = tasks.LambdaInvoke.jsonata(
            steps,
            "Publish",
            lambda_function=self.publish.function,
            payload=payload(
                {
                    "status": "READY",
                    "asset": "{% $asset %}",
                    "encoded": "{% $encoded %}",
                    "registered": "{% $registered %}",
                }
            ),
            outputs="{% $states.result.Payload %}",
        )
        prepare.add_catch(duplicate, errors=["IngestInProgressError"], outputs="{% $states.errorOutput %}")
        for step in (prepare, transcode, finalize, register, publish):
            step.add_catch(failed, outputs="{% $states.errorOutput %}")
        return prepare.next(transcode).next(finalize).next(register).next(publish)

    def _trigger(self, default_ladder_version: str) -> FastFunction:
        trigger = FastFunction(
            self,
            "Trigger",
            handler=f"{FUNCTIONS}.trigger.handler",
            description="Ingest: start an execution for an upload or an Ingest Requested event",
            environment={
                "FAST_PREFIX": self.prefix,
                "INGEST_STATE_MACHINE_ARN": self.state_machine.state_machine_arn,
                "DEFAULT_LADDER_VERSION": default_ladder_version,
                "EVENT_BUS": self.event_bus.event_bus_name,
            },
            log_retention=self.log_retention,
            removal_policy=self.removal_policy,
        )
        fn = trigger.function
        fn.configure_async_invoke(
            on_failure=destinations.SqsDestination(self.dead_letter_queue),
            retry_attempts=2,
            max_event_age=EVENT_MAX_AGE,
        )
        nag.acknowledge(trigger, {nag.LAMBDA_DLQ: "Failed asynchronous invocations go to the DeadLetters queue"})
        trigger.allow(
            ["s3:GetObject", "s3:GetObjectTagging"],
            [self.ingest_bucket.arn_for_objects("*")],
            why="Uploads and their sidecars may have any key",
        )
        # ListBucket turns a missing sidecar into a 404 instead of a 403.
        trigger.allow(["s3:ListBucket"], [self.ingest_bucket.bucket_arn])
        self.state_machine.grant_start_execution(fn)
        self.event_bus.grant_put_events_to(fn)

        # Failed deliveries (throttling, permissions) are retried by EventBridge, then dead-lettered.
        target = targets.LambdaFunction(
            fn, dead_letter_queue=self.dead_letter_queue, retry_attempts=8, max_event_age=EVENT_MAX_AGE
        )
        extensions = [events.Match.suffix_equals_ignore_case(extension) for extension in VIDEO_EXTENSIONS]
        events.Rule(
            self,
            "Uploads",
            description=f"FAST Channels ({self.prefix}): a video upload to the ingest bucket starts an ingest",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={
                    "bucket": {"name": [self.ingest_bucket.bucket_name]},
                    "object": {"key": events.Match.any_of(*extensions)},
                },
            ),
            targets=[target],
        )
        events.Rule(
            self,
            "Requests",
            description=f"FAST Channels ({self.prefix}): an Ingest Requested event starts an ingest",
            event_bus=self.event_bus,
            event_pattern=events.EventPattern(
                source=[event_source(self.prefix)], detail_type=[DETAIL_TYPES[IngestRequest]]
            ),
            targets=[target],
        )
        return trigger
