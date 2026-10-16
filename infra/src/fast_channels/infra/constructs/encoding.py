"""Encoding: ladders as MediaConvert job templates, the ingest pipeline and ladder kits (SPEC §5.1-5.2).

* :class:`EncodingLadder` - a ladder version's ``CfnJobTemplate``, compiled by ``core.mediaconvert``.
* :class:`IngestPipeline` - the MediaConvert queue and role and the Standard JSONata state machine
  Prepare -> Transcode -> Finalize -> Register -> Publish, started by uploads to the ingest bucket, by
  ``Ingest Requested`` events and by StartExecution (tools, kit provisioning).
* :class:`LadderProvisioner` - the state machine that encodes a ladder's kit (filler, break slates, house ads and
  their VAST pod) as IngestPipeline child executions.
* :class:`LadderKit` - a deploy-time custom resource that provisions a built-in ladder's kit.
"""

import hashlib
import string
from collections.abc import Mapping, Sequence
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from aws_cdk import ArnFormat, CustomResource, Duration, Fn, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_logs as logs
from aws_cdk import aws_mediaconvert as mediaconvert
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from aws_cdk import custom_resources as cr
from constructs import Construct, IConstruct

from fast_channels.core.events import DETAIL_TYPES, event_source
from fast_channels.core.ingest import IngestRequest
from fast_channels.core.mediaconvert import job_template_props
from fast_channels.core.timeline import BREAK_SLATE_GOPS
from fast_channels.functions.ingest.library import job_template_name
from fast_channels.infra import nag
from fast_channels.infra.bundling import REPO_ROOT
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.operator.nag_references import acknowledge_references

if TYPE_CHECKING:
    from fast_channels.core.ladder import Ladder

BRAND_DIR: Final = REPO_ROOT / "assets" / "brand"
"""Brand stills of every kit (rendered by ``scripts/render_brand_assets.py``)."""
BRAND_PREFIX: Final = "brand/"
"""Ingest-bucket prefix of the deployed brand stills: ``brand/<digest>/<file>.png``."""
VIDEO_EXTENSIONS: Final = (".mp4", ".m4v", ".mov", ".mxf", ".mkv", ".webm", ".avi", ".ts", ".m2ts", ".mts", ".mpg")
"""Uploads that start an ingest (MediaConvert input containers), matched case-insensitively."""
EXECUTION_TIMEOUT: Final = Duration.hours(24)
STEP_TIMEOUT: Final = Duration.minutes(1)
TRANSCODE_TIMEOUT: Final = Duration.hours(12)
EVENT_MAX_AGE: Final = Duration.hours(6)
_FUNCTIONS: Final = "fast_channels.functions.ingest"
_SYNC: Final = "Invoked synchronously by Step Functions, which retries and catches its failures"
_PROVIDER: Final = "Invoked synchronously by the custom-resource Provider framework, which reports failures"
_XRAY: Final = ("xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets")


class EncodingLadder(Construct):
    """The MediaConvert job template of one ladder version.

    The settings come from ``core.mediaconvert.compile_job_settings``, without Inputs or Destination: every job
    sends those as sparse overrides, which MediaConvert deep-merges into the template (ARCHITECTURE-SPEC App. A).
    The name ``<prefix>-<ladder id>_<fingerprint>`` is create-only, so a new ladder version is a new template.
    ``Category`` is the deployment prefix, and ``fast:owner=iac`` keeps the runtime-resource reaper away from it.

    Args:
        ladder: the validated ladder.
        prefix: deployment prefix.
        queue_arn: on-demand queue of the jobs (:attr:`IngestPipeline.queue_arn`).

    Attributes:
        ladder: the ladder.
        version: ``<id>@<fingerprint>``.
        template_name: the job template name (a plain string).
        job_template: the ``CfnJobTemplate``.
    """

    def __init__(self, scope: Construct, id: str, *, ladder: Ladder, prefix: str, queue_arn: str) -> None:
        super().__init__(scope, id)
        self.ladder = ladder
        self.version = ladder.version
        self.template_name = job_template_name(prefix, ladder.version)
        props = job_template_props(ladder, name=self.template_name, queue_arn=queue_arn, category=prefix)
        self.job_template = mediaconvert.CfnJobTemplate(
            self,
            "Resource",
            name=props["Name"],
            description=props["Description"],
            category=props["Category"],
            queue=props["Queue"],
            priority=props["Priority"],
            status_update_interval=props["StatusUpdateInterval"],
            acceleration_settings=mediaconvert.CfnJobTemplate.AccelerationSettingsProperty(
                mode=props["AccelerationSettings"]["Mode"]
            ),
            settings_json=cfn_settings(props["Settings"]),
            tags={**props["Tags"], "fast:owner": "iac"},
        )


class IngestPipeline(Construct):
    """Encodes assets with a ladder and registers them with Channel Assembly (SPEC §5.1).

    A Standard state machine (JSONata; ALL-level logs with execution data to
    ``/aws/vendedlogs/states/<prefix>-ingest``; X-Ray) takes an IngestRequest
    (``contracts/ingest-request.schema.json``) and runs ``Prepare`` (probe, refusals, sparse job overrides, item
    INGESTING) -> ``Transcode`` (MediaConvert CreateJob with the run-a-job integration; Role and Queue are literals,
    the job template and overrides come from Prepare) -> ``Finalize`` (verify outputs, AV1 av1C, nominal
    BANDWIDTH, break offsets; the SGAI copy of ads) -> ``Register`` (a Map with MaxConcurrency 1 creating the VOD
    source; empty for ads) -> ``Publish`` (item READY, ``Asset Ready``). Every step catches into
    ``PublishFailure`` (item FAILED, ``Asset Failed``) and a Fail state; an execution that finds the same encode
    already in progress stops at ``AlreadyIngesting`` without touching the item.

    Uploads with a video extension to the ingest bucket and ``Ingest Requested`` events on ``event_bus`` invoke a
    trigger function (retries, then :attr:`dead_letter_queue`) that starts executions named after the request's
    correlation id. Tools and the ladder provisioner call StartExecution with ``name=correlation_id`` themselves.

    Args:
        prefix: deployment prefix (names, ``fast:app``, MediaConvert ``UserMetadata.app``, event source).
        ingest_bucket: mezzanines and their sidecars; MediaConvert and Probe read it.
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
        role_arn: the MediaConvert role ARN (MediaExports ``mediaconvert_role_arn``).
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
            tags={"fast:app": prefix, "fast:owner": "iac"},
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
        acknowledge_references(
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

        self.dead_letter_queue = sqs.Queue(
            self,
            "DeadLetters",
            retention_period=Duration.days(14),
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            enforce_ssl=True,
            removal_policy=removal_policy,
        )
        nag.acknowledge(
            self.dead_letter_queue,
            {
                nag.SQS_DLQ: "This is the pipeline's dead-letter queue",
                "Serverless-SQSRedrivePolicy": "This is the pipeline's dead-letter queue",
            },
        )
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
            handler=f"{_FUNCTIONS}.{id.lower()}.handler",
            description=description,
            environment=environment,
            timeout=timeout,
            memory_size=memory_size,
            log_retention=self.log_retention,
            removal_policy=self.removal_policy,
        )
        nag.acknowledge(step, {nag.LAMBDA_DLQ: _SYNC})
        return step

    def _mediaconvert_role(self) -> iam.Role:
        stack = Stack.of(self)
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
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnEquals": {"aws:SourceArn": self.queue_arn},
                },
            ),
        )
        _allow(role, ["s3:GetObject"], [self.ingest_bucket.arn_for_objects("*")], why="Mezzanines may have any key")
        _allow(
            role,
            ["s3:PutObject"],
            [self.media_bucket.arn_for_objects("assets/*")],
            why="Encodes go to assets/<asset>/<ladder>/",
        )
        return role

    def _grant_steps(self) -> None:
        prepare, finalize, register, publish = (
            _role(step) for step in (self.prepare, self.finalize, self.register, self.publish)
        )
        library, assets = [self.library.table_arn], [self.media_bucket.arn_for_objects("assets/*")]

        _allow(prepare, ["mediaconvert:Probe"], ["*"], why="mediaconvert:Probe has no resource-level permissions")
        # Probe reads the source with the caller's credentials (mediaconvert.md §7.3).
        _allow(prepare, ["s3:GetObject"], [self.ingest_bucket.arn_for_objects("*")], why="Mezzanines of any key")
        _allow(prepare, ["dynamodb:GetItem", "dynamodb:PutItem"], library)
        # Whether the execution holding an INGESTING item still runs (a literal ARN: the machine needs this role).
        executions = Stack.of(self).format_arn(
            service="states",
            resource="execution",
            resource_name=f"{self.prefix}-ingest:*",
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )
        _allow(prepare, ["states:DescribeExecution"], [executions], why="Executions of the ingest state machine")

        _allow(finalize, ["s3:GetObject", "s3:PutObject"], assets, why="Manifests and init segments of any encode")
        _allow(finalize, ["s3:PutObject"], [self.media_bucket.arn_for_objects("ads/*")], why="SGAI-ready ad manifests")
        # ListBucket turns a missing manifest into a 404 instead of a 403.
        _allow(
            finalize,
            ["s3:ListBucket"],
            [self.media_bucket.bucket_arn],
            conditions={"StringLike": {"s3:prefix": ["assets/*"]}},
        )
        _allow(finalize, ["dynamodb:UpdateItem"], library)

        stack = Stack.of(self)
        vod_sources = stack.format_arn(
            service="mediatailor", resource="vodSource", resource_name=f"{self.source_location_name}/*"
        )
        _allow(
            register,
            [
                "mediatailor:DescribeVodSource",
                "mediatailor:CreateVodSource",
                "mediatailor:UpdateVodSource",
                "mediatailor:TagResource",
            ],
            [vod_sources],
            why="VOD sources of the deployment's source location",
        )
        # ListAlerts is authorized on the account's alerts resource, not on the resource queried (observed).
        alerts = stack.format_arn(service="mediatailor", resource="alerts", resource_name="*")
        _allow(register, ["mediatailor:ListAlerts"], [alerts], why="ListAlerts is authorized on alerts/* only")
        # CreateVodSource checks that the caller itself can read the manifests (channel-assembly.md §2.3).
        _allow(register, ["s3:GetObject"], assets, why="Manifests of any encode")

        _allow(publish, ["dynamodb:UpdateItem"], library)
        self.event_bus.grant_put_events_to(publish)

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
                "AccelerationSettings": "{% $asset.job.AccelerationSettings %}",
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
            handler=f"{_FUNCTIONS}.trigger.handler",
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
        role = _role(trigger)
        _allow(
            role,
            ["s3:GetObject", "s3:GetObjectTagging"],
            [self.ingest_bucket.arn_for_objects("*")],
            why="Uploads and their sidecars may have any key",
        )
        # ListBucket turns a missing sidecar into a 404 instead of a 403.
        _allow(role, ["s3:ListBucket"], [self.ingest_bucket.bucket_arn])
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


class LadderProvisioner(Construct):
    """Encodes a ladder version's kit through the ingest pipeline (SPEC §5.2).

    A Standard JSONata state machine (logs ``/aws/vendedlogs/states/<prefix>-ladder-provisioner``, X-Ray):
    ``Begin`` records the ladder as PROVISIONING and lists its kit (the filler slate, a break slate per
    ``BREAK_SLATE_GOPS`` length and the house ads); ``EncodeKit`` runs each as an ingest child execution
    (``startExecution.sync:2``, linked to the parent, named ``<execution>-<label>``); ``Complete`` writes
    ``ads/<ladder>/vast.xml`` and ``ads/b.gif``, marks the ladder READY with its kit and publishes
    ``Ladder Ready``. A failure marks it FAILED and publishes ``Ladder Failed``.

    Input: ``{"ladder": <Ladder JSON>, "job_template": <name>, "kit_fingerprint"?, "owner"?: "iac"|"agent",
    "correlation_id"?}``; the job template must already exist. The brand stills (``assets/brand``) are deployed to
    ``brand/<digest>/`` of the ingest bucket, which the MediaConvert role already reads.

    Args:
        pipeline: the ingest pipeline that encodes the kit.
        max_parallel_encodes: kit assets encoded at the same time.

    Attributes:
        state_machine: the provisioner (MediaExports ``ladder_provisioner``).
        brand_digest: hash of the brand stills (part of every kit's fingerprint).
        brand: the deployment of the stills (kits must run after it).
        kit: the kit-steps function (``FastFunction``).
    """

    def __init__(self, scope: Construct, id: str, *, pipeline: IngestPipeline, max_parallel_encodes: int = 4) -> None:
        super().__init__(scope, id)
        self.pipeline = pipeline
        self.brand_digest = brand_digest()
        self.brand = self._deploy_brand()
        stills = f"s3://{pipeline.ingest_bucket.bucket_name}/{BRAND_PREFIX}{self.brand_digest}/"
        self.images = {
            "filler": f"{stills}filler.png",
            "break_slate": f"{stills}break.png",
            "house_ads": [f"{stills}house-ad-1.png", f"{stills}house-ad-2.png"],
        }
        self.kit = FastFunction(
            self,
            "Kit",
            handler=f"{_FUNCTIONS}.kit.handler",
            description="Ladder kit: record the ladder, write its VAST pod, mark it READY or FAILED",
            environment={
                "FAST_PREFIX": pipeline.prefix,
                "LIBRARY_TABLE": pipeline.library.table_name,
                "MEDIA_BUCKET": pipeline.media_bucket.bucket_name,
                "CONTENT_BASE_URL": pipeline.content_base_url,
                "EVENT_BUS": pipeline.event_bus.event_bus_name,
            },
            log_retention=pipeline.log_retention,
            removal_policy=pipeline.removal_policy,
        )
        nag.acknowledge(self.kit, {nag.LAMBDA_DLQ: _SYNC})
        role = _role(self.kit)
        _allow(role, ["dynamodb:UpdateItem"], [pipeline.library.table_arn])
        _allow(
            role, ["s3:PutObject"], [pipeline.media_bucket.arn_for_objects("ads/*")], why="VAST pods of every ladder"
        )
        pipeline.event_bus.grant_put_events_to(role)

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/vendedlogs/states/{pipeline.prefix}-ladder-provisioner",
            retention=pipeline.log_retention,
            removal_policy=pipeline.removal_policy,
        )
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_name=f"{pipeline.prefix}-ladder-provisioner",
            comment="Encode a ladder version's filler, break slates and house ads, then mark it READY",
            query_language=sfn.QueryLanguage.JSONATA,
            definition_body=sfn.DefinitionBody.from_chainable(self._definition(max_parallel_encodes)),
            logs=sfn.LogOptions(destination=self.log_group, level=sfn.LogLevel.ALL, include_execution_data=True),
            tracing_enabled=True,
            timeout=Duration.hours(2),
        )
        acknowledge_references(
            self.state_machine.role,
            {
                nag.wildcard_resource(): "X-Ray has no resource-level permissions",
                nag.wildcard_resource(f"{self.kit.function.function_arn}:*"): "Invoke the step and its versions",
                nag.wildcard_resource(f"{_executions(pipeline.state_machine)}*"): "Describe and stop the kit's "
                "ingest executions, whose names are only known at run time",
            },
        )

    def _deploy_brand(self) -> s3deploy.BucketDeployment:
        stills = s3_assets.Asset(self, "BrandStills", path=str(BRAND_DIR))
        deployment = s3deploy.BucketDeployment(
            self,
            "Brand",
            sources=[s3deploy.Source.bucket(stills.bucket, stills.s3_object_key)],
            destination_bucket=self.pipeline.ingest_bucket,
            destination_key_prefix=f"{BRAND_PREFIX}{self.brand_digest}/",
            retain_on_delete=False,
        )
        # The CDK-managed handler is a stack-wide singleton; its findings are acknowledged where it lives.
        handler = cast("IConstruct", cast("IConstruct", deployment.handler_role).node.scope)
        reason = "CDK BucketDeployment handler (aws-cdk-lib shapes it)"
        acknowledge_references(
            handler,
            {
                nag.LAMBDA_DLQ: f"{reason}, invoked synchronously by CloudFormation",
                "Serverless-LambdaLatestVersion": f"{reason}: aws-cdk-lib chooses its runtime",
                "AwsSolutions-L1": f"{reason}: aws-cdk-lib chooses its runtime",
                nag.managed_policy("service-role/AWSLambdaBasicExecutionRole"): f"{reason}: its role",
                nag.wildcard_resource(): f"{reason}: X-Ray tracing has no resource-level permissions",
                nag.wildcard_resource(stills.bucket.arn_for_objects("*")): f"{reason}: reads the stills asset",
                nag.wildcard_resource(self.pipeline.ingest_bucket.arn_for_objects("*")): f"{reason}: writes "
                "brand/<digest>/ of the ingest bucket",
                **{
                    nag.wildcard_action(action): f"{reason}: copies the stills"
                    for action in ("s3:GetBucket*", "s3:GetObject*", "s3:List*", "s3:Abort*", "s3:DeleteObject*")
                },
            },
        )
        return deployment

    def _definition(self, max_parallel_encodes: int) -> sfn.IChainable:
        steps = Construct(self, "Steps")
        payload = sfn.TaskInput.from_object
        kit = self.kit.function
        failed = tasks.LambdaInvoke.jsonata(
            steps,
            "RecordFailure",
            lambda_function=kit,
            payload=payload(
                {
                    "step": "failed",
                    "input": "{% $states.context.Execution.Input %}",
                    "execution": "{% $states.context.Execution.Name %}",
                    "error": "{% $states.input %}",
                }
            ),
            outputs="{% $states.result.Payload %}",
        ).next(
            sfn.Fail.jsonata(
                steps, "ProvisioningFailed", error="{% $states.input.error %}", cause="{% $states.input.cause %}"
            )
        )
        begin = tasks.LambdaInvoke.jsonata(
            steps,
            "Begin",
            lambda_function=kit,
            payload=payload(
                {
                    "step": "begin",
                    "input": "{% $states.input %}",
                    "execution": "{% $states.context.Execution.Name %}",
                    "images": self.images,
                }
            ),
            assign={"kit": "{% $states.result.Payload %}"},
        )
        ingest = tasks.StepFunctionsStartExecution.jsonata(
            steps,
            "Ingest",
            state_machine=self.pipeline.state_machine,
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            associate_with_parent=True,
            name="{% $states.input.correlation_id %}",
            input=payload(
                {
                    "generator": "{% $states.input.generator %}",
                    "kind": "{% $states.input.kind %}",
                    "ladder_version": "{% $states.input.ladder_version %}",
                    "correlation_id": "{% $states.input.correlation_id %}",
                }
            ),
            outputs="{% $states.result.Output %}",
        )
        encode = sfn.Map.jsonata(
            steps,
            "EncodeKit",
            items=sfn.ProvideItems.jsonata("{% $kit.requests %}"),
            max_concurrency=max_parallel_encodes,
            assign={"assets": "{% $states.result %}"},
        )
        encode.item_processor(ingest)
        complete = tasks.LambdaInvoke.jsonata(
            steps,
            "Complete",
            lambda_function=kit,
            payload=payload({"step": "ready", "kit": "{% $kit %}", "assets": "{% $assets %}"}),
            outputs="{% $states.result.Payload %}",
        )
        for step in (begin, encode, complete):
            step.add_catch(failed, outputs="{% $states.errorOutput %}")
        return begin.next(encode).next(complete)

    @cached_property
    def kit_provider(self) -> cr.Provider:
        """The asynchronous Provider of :class:`LadderKit` resources (created with the first kit)."""
        pipeline = self.pipeline
        on_event = self._kit_handler("KitOnEvent", "on_event", "Custom::LadderKit: start provisioning unless current")
        is_complete = self._kit_handler("KitIsComplete", "is_complete", "Custom::LadderKit: poll the provisioning")
        on_event.function.add_environment("PROVISION_STATE_MACHINE_ARN", self.state_machine.state_machine_arn)
        self.state_machine.grant_start_execution(on_event.function)
        self.state_machine.grant_execution(is_complete.function, "states:DescribeExecution")
        acknowledge_references(
            _role(is_complete),
            {nag.wildcard_resource(f"{_executions(self.state_machine)}:*"): "Poll provisioning executions"},
        )
        waiter_logs = logs.LogGroup(
            self,
            "KitWaiterLogs",
            log_group_name=f"/aws/vendedlogs/states/{pipeline.prefix}-ladder-kit-waiter",
            retention=pipeline.log_retention,
            removal_policy=pipeline.removal_policy,
        )
        provider = cr.Provider(
            self,
            "KitProvider",
            on_event_handler=on_event.function,
            is_complete_handler=is_complete.function,
            query_interval=Duration.seconds(30),
            total_timeout=Duration.hours(1),
            disable_waiter_state_machine_logging=False,
            waiter_state_machine_log_options=cr.LogOptions(
                destination=waiter_logs, level=sfn.LogLevel.ALL, include_execution_data=True
            ),
        )
        _trace_waiter(provider)
        _acknowledge_framework(provider, on_event.function, is_complete.function)
        return provider

    def _kit_handler(self, id: str, name: str, description: str) -> FastFunction:
        handler = FastFunction(
            self,
            id,
            handler=f"fast_channels.functions.custom_resources.ladder_kit.{name}",
            description=description,
            environment={"LIBRARY_TABLE": self.pipeline.library.table_name},
            log_retention=self.pipeline.log_retention,
            removal_policy=self.pipeline.removal_policy,
        )
        nag.acknowledge(handler, {nag.LAMBDA_DLQ: _PROVIDER})
        _allow(_role(handler), ["dynamodb:GetItem"], [self.pipeline.library.table_arn])
        return handler


class LadderKit(Construct):
    """``Custom::LadderKit``: provisions a built-in ladder's kit at deploy time.

    Create and Update start the provisioner unless the ladder version is READY with the same kit fingerprint (a
    hash of the resolved properties: ladder, brand stills, content host, package profiles, source location), so
    redeploys take seconds; the Provider polls the execution for up to an hour. Delete is a no-op: the kit's VOD
    sources are runtime resources, removed by the RuntimeResourceReaper. The kit needs the source location and the
    bucket policy that admits it, so make the kit depend on the Channel Assembly origin
    (``kit.node.add_dependency(origin)``).

    Args:
        ladder: the built-in ladder's job template.
        provisioner: the ladder provisioner.

    Attributes:
        ladder_version: ``<id>@<fingerprint>`` (a plain string).
        filler_vod_source: VOD source of the filler slate (``FillerSlate`` of the ladder's channels).
        break_slate_vod_sources: VOD source per break-slate length in GOPs (``AdBreaks[].Slate``).
        house_ads: comma-separated creative ids of the house ads.
        vast_key: media-bucket key of the VAST pod.
        vast_url: URL of the VAST pod on the content host.
    """

    def __init__(self, scope: Construct, id: str, *, ladder: EncodingLadder, provisioner: LadderProvisioner) -> None:
        super().__init__(scope, id)
        pipeline = provisioner.pipeline
        self.ladder_version = ladder.version
        self.resource = CustomResource(
            self,
            "Resource",
            service_token=provisioner.kit_provider.service_token,
            resource_type="Custom::LadderKit",
            properties={
                "LadderVersion": ladder.version,
                "Ladder": ladder.ladder.model_dump_json(),
                "JobTemplate": ladder.template_name,
                "BrandDigest": provisioner.brand_digest,
                "ContentBaseUrl": pipeline.content_base_url,
                "PackageProfiles": ",".join(pipeline.package_profiles),
                "SourceLocation": pipeline.source_location_name,
            },
        )
        self.resource.node.add_dependency(ladder, provisioner.brand)
        self.filler_vod_source = self.resource.get_att_string("FillerVodSource")
        self.break_slate_vod_sources: Mapping[int, str] = {
            gops: self.resource.get_att_string(f"BreakSlate{gops}") for gops in BREAK_SLATE_GOPS
        }
        self.house_ads = self.resource.get_att_string("HouseAds")
        self.vast_key = self.resource.get_att_string("VastKey")
        self.vast_url = self.resource.get_att_string("VastUrl")


def cfn_settings(settings: Any) -> Any:
    """Job settings (API shape) with the member names CloudFormation's ``AWS::MediaConvert::JobTemplate`` handler
    expects in ``SettingsJson``.

    The handler binds ``SettingsJson`` to the Java SDK v2 builders, whose bean names lower-case a leading acronym
    after its first letter: ``CmfcSettings.IFrameOnlyManifest`` is rejected as unrecognized and must be
    ``IframeOnlyManifest`` (observed in a deployment; CreateJobTemplate takes the API names). It is the only such
    member in the compiled ladders; capitals inside a name (``GopBReference``) keep their spelling.
    """
    match settings:
        case dict():
            return {_java_bean_name(k): cfn_settings(v) for k, v in settings.items()}
        case list():
            return [cfn_settings(v) for v in settings]
        case _:
            return settings


def _java_bean_name(member: str) -> str:
    acronym = len(member) - len(member.lstrip(string.ascii_uppercase))
    return member if acronym < 2 else member[0] + member[1:acronym].lower() + member[acronym:]


def brand_digest(directory: Path = BRAND_DIR) -> str:
    """Hash of the brand stills (file names and bytes), 10 hex characters."""
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.png")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _role(function: FastFunction) -> iam.IRole:
    return cast("iam.IRole", function.function.role)


def _allow(
    role: iam.IRole,
    actions: Sequence[str],
    resources: Sequence[str],
    *,
    why: str | None = None,
    conditions: Mapping[str, Any] | None = None,
) -> None:
    """Allow ``actions`` on ``resources``; ``why`` explains its wildcard resources to cdk-nag."""
    role.add_to_principal_policy(
        iam.PolicyStatement(actions=list(actions), resources=list(resources), conditions=conditions)
    )
    if why is not None:
        acknowledge_references(role, {nag.wildcard_resource(r): why for r in resources if "*" in r})


def _executions(state_machine: sfn.IStateMachine) -> str:
    """``arn:...:execution:<state machine name>``, as the CDK Step Functions grants write it."""
    return Stack.of(state_machine).format_arn(
        service="states",
        resource="execution",
        resource_name=Fn.select(6, Fn.split(":", state_machine.state_machine_arn)),
        arn_format=ArnFormat.COLON_RESOURCE_NAME,
    )


def _trace_waiter(provider: cr.Provider) -> None:
    """X-Ray tracing for the Provider's waiter state machine, whose L2 offers logging options only."""
    waiter = provider.node.find_child("waiter-state-machine")
    cast(
        "sfn.CfnStateMachine", waiter.node.find_child("Resource")
    ).tracing_configuration = sfn.CfnStateMachine.TracingConfigurationProperty(enabled=True)
    role = cast("iam.Role", waiter.node.find_child("Role"))
    role.add_to_principal_policy(iam.PolicyStatement(actions=list(_XRAY), resources=["*"]))


def _acknowledge_framework(provider: cr.Provider, *handlers: lambda_.IFunction) -> None:
    """cdk-nag findings of the CDK Provider framework, whose functions and roles this module does not shape.

    The framework (and its waiter) invoke ``handlers`` and the framework functions as ``<function ARN>:*``.
    """
    invoked = [*handlers, *(c for c in provider.node.find_all() if isinstance(c, lambda_.Function))]
    acknowledge_references(
        provider,
        {
            nag.LAMBDA_DLQ: _PROVIDER,
            nag.managed_policy("service-role/AWSLambdaBasicExecutionRole"): "Provider framework function role",
            nag.wildcard_resource(): "X-Ray tracing, and the waiter's CloudWatch Logs delivery actions, have no "
            "resource-level permissions",
            "Serverless-LambdaDefaultMemorySize": "Provider framework functions only relay requests",
            **{
                nag.wildcard_resource(f"{function.function_arn}:*"): "Provider framework: invoke the function and "
                "its versions"
                for function in invoked
            },
        },
    )
