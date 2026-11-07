"""The understanding workflow: one Step Functions execution per title, on its first ``Asset Ready``.

``Prepare`` (Lambda: the title, the proxy job, the keys; or skip) -> ``Proxy`` (MediaConvert ``CreateJob``, run-a-job)
-> ``Analyses`` (a Parallel state: each configured analysis is started by an AWS SDK integration and polled every
30 s; a failed one becomes an error entry, never a failed execution) -> ``Collect`` (Lambda: the results reduced to
signals) -> ``Describe`` (Lambda: the model-registry call, the item and the events).

OPTIMIZATION(aws): EI-VOD: AWS Elemental Inference has no VOD analysis, so this workflow glues MediaConvert,
Transcribe, Rekognition Video and a Bedrock model together. When Elemental Inference analyses files, its job
replaces ``Proxy``, ``Analyses`` and ``Describe``'s model call behind the same ``UNDERSTANDING`` item.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_mediaconvert as mediaconvert
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from fast_channels.core.events import DETAIL_TYPES, AssetReady, event_source
from fast_channels.core.ingest import AssetKind
from fast_channels.core.tags import Owner, resource_tags
from fast_channels.core.understanding.record import Analysis
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.alerts import PERIOD, alarm
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.models.caps import grant_counting
from fast_channels.infra.constructs.models.profiles import foundation_model, task_invoke_statements
from fast_channels.infra.constructs.models.registry import READ_ACTIONS, entry_arn
from fast_channels.infra.constructs.queues import dead_letter_queue

if TYPE_CHECKING:
    from aws_cdk import aws_sns as sns

    from fast_channels.infra.exports import DataExports, TaskProfile

FUNCTIONS: Final = "fast_channels.functions.understanding.steps"
POLL: Final = Duration.seconds(30)
PROXY_TIMEOUT: Final = Duration.hours(2)
EXECUTION_TIMEOUT: Final = Duration.hours(8)
"""Rekognition and Transcribe take about a third of a title's runtime each; a long film fits several times over."""
PROXY_DAYS: Final = 1
RESULTS_DAYS: Final = 30
MIN_CONFIDENCE: Final = 80
"""Rekognition's own floor for labels, moderation labels and segments (the signals use the same value)."""
CONCURRENT_JOBS: Final = 5
STEP_TIMEOUT: Final = Duration.minutes(1)


class UnderstandingWorkflow(Construct):
    """The bucket, the MediaConvert queue and role, the three step functions, the state machine, its trigger and
    its failure alarm.

    Args:
        prefix: deployment prefix.
        data: Data stack references (the media library and catalog tables, the media and logs buckets).
        event_bus: the deployment's bus (the trigger, and the events ``Describe`` puts).
        alert_topic: where the failure alarm goes, and the model task's cap notice.
        profile: the model task's application inference profile (Models stack).
        task: the model task (``modules.understanding.task``).
        analyses: the analyses to run (``modules.understanding.analyses``).
        log_retention, removal_policy: of the logs, the queue and the bucket.

    Attributes:
        bucket, queue, state_machine, rule, alarm; prepare, collect, describe: the step functions.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        data: DataExports,
        event_bus: events.IEventBus,
        alert_topic: sns.ITopic,
        profile: TaskProfile,
        task: str,
        analyses: Sequence[Analysis],
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.prefix = prefix
        self.analyses = tuple(analyses)
        self.bucket = s3.Bucket(
            self,
            "Bucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            minimum_tls_version=1.2,
            removal_policy=removal_policy,
            auto_delete_objects=removal_policy == RemovalPolicy.DESTROY,
            lifecycle_rules=[
                s3.LifecycleRule(id="expire-proxies", prefix="proxies/", expiration=Duration.days(PROXY_DAYS)),
                s3.LifecycleRule(id="expire-results", prefix="results/", expiration=Duration.days(RESULTS_DAYS)),
                s3.LifecycleRule(id="abort-uploads", abort_incomplete_multipart_upload_after=Duration.days(1)),
            ],
        )
        nag.acknowledge(
            self.bucket,
            {
                nag.S3_SERVER_ACCESS_LOGS: "Only the workflow writes and reads it, through its own roles and the jobs "
                "it starts, which its execution history records; objects expire after 1 and 30 days. The Data "
                "stack's logs bucket accepts server access logs from its own buckets only."
            },
        )
        self.queue = mediaconvert.CfnQueue(
            self,
            "Queue",
            name=f"{prefix}-understanding",
            description=f"FAST Channels ({prefix}): analysis proxies of the understanding module",
            pricing_plan="ON_DEMAND",
            concurrent_jobs=CONCURRENT_JOBS,
            status="ACTIVE",
            tags=resource_tags(prefix, Owner.IAC),
        )
        self.role = self._mediaconvert_role(data)
        self.prepare = self._step(
            "Prepare",
            "Understanding: the title, its proxy job and its keys",
            {
                "LIBRARY_TABLE": data.library_table.table_name,
                "CATALOG_TABLE": data.catalog_table.table_name,
                "MEDIA_BUCKET": data.media_bucket.bucket_name,
            },
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        self.collect = self._step(
            "Collect",
            "Understanding: the analyses' results, reduced to signals",
            {},
            log_retention=log_retention,
            removal_policy=removal_policy,
            timeout=Duration.minutes(5),
            memory_size=1024,
        )
        self.describe = self._step(
            "Describe",
            "Understanding: the model's description, the UNDERSTANDING item and its events",
            {
                "CATALOG_TABLE": data.catalog_table.table_name,
                "EVENT_BUS": event_bus.event_bus_name,
                "UNDERSTANDING_TASK": task,
                "MODELS_COUNTER_TABLE": f"{prefix}-models",
                "ALERT_TOPIC_ARN": alert_topic.topic_arn,
            },
            log_retention=log_retention,
            removal_policy=removal_policy,
            timeout=Duration.minutes(10),
        )
        self._grant(data, event_bus, alert_topic, profile, task)
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/vendedlogs/states/{prefix}-understanding",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_name=f"{prefix}-understanding",
            comment="Analyse a title once: proxy, Transcribe and Rekognition, then the model's description",
            query_language=sfn.QueryLanguage.JSONATA,
            definition_body=sfn.DefinitionBody.from_chainable(self._definition()),
            logs=sfn.LogOptions(destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=False),
            tracing_enabled=True,
            timeout=EXECUTION_TIMEOUT,
        )
        self._grant_machine()
        self.rule = self._trigger(event_bus, removal_policy)
        self.alarm = alarm(
            self,
            "FailedAlarm",
            name=f"{prefix}-understanding-failed",
            metric=self.state_machine.metric_failed(period=PERIOD, statistic=cw.Stats.SUM),
            threshold=1,
            description="An understanding execution failed: its title has no UNDERSTANDING item (see the execution)",
            topic=alert_topic,
        )

    # --- functions and grants --------------------------------------------------------------------------------------

    def _step(
        self,
        id: str,
        description: str,
        environment: dict[str, str],
        *,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy,
        timeout: Duration = STEP_TIMEOUT,
        memory_size: int = 256,
    ) -> FastFunction:
        step = FastFunction(
            self,
            id,
            handler=f"{FUNCTIONS}.{id.lower()}",
            description=description,
            environment={
                "FAST_PREFIX": self.prefix,
                "UNDERSTANDING_BUCKET": self.bucket.bucket_name,
                "UNDERSTANDING_ANALYSES": ",".join(a.value for a in self.analyses),
                **environment,
            },
            timeout=timeout,
            memory_size=memory_size,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        nag.acknowledge(step, {nag.LAMBDA_DLQ: "Invoked by the state machine, which records a failure"})
        return step

    def _mediaconvert_role(self, data: DataExports) -> iam.Role:
        role = iam.Role(
            self,
            "MediaConvertRole",
            description=f"FAST Channels ({self.prefix}): MediaConvert reads encodes and writes analysis proxies",
            # Only jobs on this module's queue may assume the role (MediaConvert presents the queue as aws:SourceArn).
            assumed_by=iam.ServicePrincipal(
                "mediaconvert.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Stack.of(self).account},
                    "ArnEquals": {"aws:SourceArn": self.queue.attr_arn},
                },
            ),
        )
        role.add_to_policy(
            iam.PolicyStatement(actions=["s3:GetObject"], resources=[data.media_bucket.arn_for_objects("assets/*")])
        )
        role.add_to_policy(
            iam.PolicyStatement(actions=["s3:PutObject"], resources=[self.bucket.arn_for_objects("proxies/*")])
        )
        nag.acknowledge_references(
            role,
            {
                nag.wildcard_resource(f"{data.media_bucket.bucket_arn}/assets/*"): "The HLS encodes of any asset",
                nag.wildcard_resource(f"{self.bucket.bucket_arn}/proxies/*"): "One proxy per title",
            },
        )
        return role

    def _grant(
        self,
        data: DataExports,
        event_bus: events.IEventBus,
        alert_topic: sns.ITopic,
        profile: TaskProfile,
        task: str,
    ) -> None:
        proxies, results = self.bucket.arn_for_objects("proxies/*"), self.bucket.arn_for_objects("results/*")
        titles = {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["CONTENT#*"]}}
        self.prepare.allow(["dynamodb:GetItem"], [data.library_table.table_arn])
        self.prepare.allow(["dynamodb:GetItem"], [data.catalog_table.table_arn], conditions=titles)
        self.prepare.allow(
            ["s3:GetObject"], [data.media_bucket.arn_for_objects("assets/*")], why="The multivariant of any encode"
        )
        self.collect.allow(
            ["rekognition:GetSegmentDetection", "rekognition:GetLabelDetection", "rekognition:GetContentModeration"],
            ["*"],
            why="Rekognition Video's Get* actions have no resource-level permissions",
        )
        self.collect.allow(["s3:GetObject", "s3:PutObject"], [results], why="One results folder per title")
        describe = self.describe
        for statement in task_invoke_statements(profile, partition=Stack.of(self).partition):
            describe.function.add_to_role_policy(statement)
        describe.allow(list(READ_ACTIONS), [entry_arn(self, prefix=self.prefix, task=task)])
        grant_counting(describe.function, self, prefix=self.prefix, task=task)
        describe.allow(["sns:Publish"], [alert_topic.topic_arn])
        # Bedrock reads the proxy (an S3 video source of Converse) with the caller's credentials.
        describe.allow(["s3:GetObject"], [proxies], why="The proxy of any title")
        describe.allow(["dynamodb:PutItem"], [data.catalog_table.table_arn], conditions=titles)
        event_bus.grant_put_events_to(describe.function)
        partition = Stack.of(self).partition
        nag.acknowledge_references(
            describe,
            {
                nag.wildcard_resource(
                    f"arn:{partition}:bedrock:*::foundation-model/{foundation_model(profile.model)}"
                ): (
                    f"Application inference profile of model task {task}: routes to the Regions of the profile it "
                    "copies; allowed only through the task's profile (bedrock:InferenceProfileArn)"
                ),
            },
        )

    def _grant_machine(self) -> None:
        machine = self.state_machine
        proxies, results = self.bucket.arn_for_objects("proxies/*"), self.bucket.arn_for_objects("results/*")
        # Rekognition and Transcribe read the proxy, and Transcribe writes its transcript, with the caller's role.
        machine.add_to_role_policy(iam.PolicyStatement(actions=["s3:GetObject"], resources=[proxies]))
        machine.add_to_role_policy(iam.PolicyStatement(actions=["s3:PutObject"], resources=[results]))
        nag.acknowledge_references(
            machine.role,
            {
                nag.wildcard_resource(): "MediaConvert jobs (their ARNs exist only after creation), Rekognition Video "
                "Start*/Get* (no resource-level permissions) and X-Ray",
                nag.wildcard_resource(f"{self.bucket.bucket_arn}/proxies/*"): "One proxy per title",
                nag.wildcard_resource(f"{self.bucket.bucket_arn}/results/*"): "One results folder per title",
                nag.wildcard_resource(arn(self, "transcribe", "transcription-job", f"{self.prefix}-*")): (
                    "The module's transcription jobs, named <prefix>-<execution>"
                ),
                **{
                    nag.wildcard_resource(f"{step.function.function_arn}:*"): "Invoke the step and its versions"
                    for step in (self.prepare, self.collect, self.describe)
                },
            },
        )

    # --- the state machine -----------------------------------------------------------------------------------------

    def _definition(self) -> sfn.IChainable:
        steps = Construct(self, "Steps")
        payload = sfn.TaskInput.from_object
        prepare = tasks.LambdaInvoke.jsonata(
            steps,
            "Prepare",
            lambda_function=self.prepare.function,
            payload=payload(
                {
                    "detail": "{% $states.input.detail %}",
                    "force": "{% $exists($states.input.force) and $states.input.force = true %}",
                    "execution": "{% $states.context.Execution.Name %}",
                }
            ),
            assign={"prepared": "{% $states.result.Payload %}"},
        )
        proxy = tasks.MediaConvertCreateJob.jsonata(
            steps,
            "Proxy",
            create_job_request={
                "Role": self.role.role_arn,
                "Queue": self.queue.attr_arn,
                "Settings": "{% $prepared.job.Settings %}",
                "UserMetadata": "{% $prepared.job.UserMetadata %}",
            },
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            task_timeout=sfn.Timeout.duration(PROXY_TIMEOUT),
        )
        proxy.add_retry(
            errors=["MediaConvert.TooManyRequestsException", "MediaConvert.InternalServerErrorException"],
            interval=Duration.seconds(5),
            max_attempts=4,
            backoff_rate=2,
            jitter_strategy=sfn.JitterType.FULL,
        )
        analyses = sfn.Parallel.jsonata(steps, "Analyses", outputs="{% $states.result %}")
        for branch in self._branches(steps):
            analyses.branch(branch)
        collect = tasks.LambdaInvoke.jsonata(
            steps,
            "Collect",
            lambda_function=self.collect.function,
            payload=payload({"prepared": "{% $prepared %}", "analyses": "{% $states.input %}"}),
            outputs="{% $states.result.Payload %}",
        )
        describe = tasks.LambdaInvoke.jsonata(
            steps,
            "Describe",
            lambda_function=self.describe.function,
            payload=payload(
                {
                    "prepared": "{% $prepared %}",
                    "signals": "{% $states.input.signals %}",
                    "failed": "{% $states.input.failed %}",
                }
            ),
            outputs="{% $states.result.Payload %}",
        )
        skipped = sfn.Succeed.jsonata(steps, "Skipped", outputs="{% $prepared %}")
        start = sfn.Choice.jsonata(steps, "AnyToAnalyse")
        start.when(sfn.Condition.jsonata("{% $prepared.skip %}"), skipped)
        start.otherwise(proxy.next(analyses).next(collect).next(describe))
        return prepare.next(start)

    def _branches(self, steps: Construct) -> list[sfn.IChainable]:
        video = {"S3Object": {"Bucket": "{% $prepared.bucket %}", "Name": "{% $prepared.proxy_key %}"}}
        branches: list[sfn.IChainable] = []
        if Analysis.TRANSCRIPT in self.analyses:
            branches.append(self._transcript(steps))
        segment_types = [
            kind
            for analysis, kind in ((Analysis.SHOTS, "SHOT"), (Analysis.TECHNICAL_CUES, "TECHNICAL_CUE"))
            if analysis in self.analyses
        ]
        if segment_types:
            covered = [a for a in (Analysis.SHOTS, Analysis.TECHNICAL_CUES) if a in self.analyses]
            filters: dict[str, Any] = {}
            if "SHOT" in segment_types:
                filters["ShotFilter"] = {"MinSegmentConfidence": MIN_CONFIDENCE}
            if "TECHNICAL_CUE" in segment_types:
                filters["TechnicalCueFilter"] = {"MinSegmentConfidence": MIN_CONFIDENCE}
            branches.append(
                self._rekognition(
                    steps,
                    "Segments",
                    covered,
                    start="startSegmentDetection",
                    get="getSegmentDetection",
                    parameters={"Video": video, "SegmentTypes": segment_types, "Filters": filters},
                )
            )
        if Analysis.LABELS in self.analyses:
            branches.append(
                self._rekognition(
                    steps,
                    "Labels",
                    [Analysis.LABELS],
                    start="startLabelDetection",
                    get="getLabelDetection",
                    parameters={"Video": video, "MinConfidence": MIN_CONFIDENCE, "Features": ["GENERAL_LABELS"]},
                )
            )
        if Analysis.MODERATION in self.analyses:
            branches.append(
                self._rekognition(
                    steps,
                    "Moderation",
                    [Analysis.MODERATION],
                    start="startContentModeration",
                    get="getContentModeration",
                    parameters={"Video": video, "MinConfidence": MIN_CONFIDENCE},
                )
            )
        return branches

    def _transcript(self, steps: Construct) -> sfn.IChainable:
        jobs = [arn(self, "transcribe", "transcription-job", f"{self.prefix}-*")]
        entry = {"analysis": Analysis.TRANSCRIPT.value, "job_id": "{% $prepared.transcription_job %}"}
        failed = sfn.Pass.jsonata(
            steps,
            "TranscriptFailed",
            outputs=[{"analysis": Analysis.TRANSCRIPT.value, "error": "{% $string($states.input) %}"}],
        )
        start = tasks.CallAwsService.jsonata(
            steps,
            "StartTranscript",
            service="transcribe",
            action="startTranscriptionJob",
            parameters={
                "TranscriptionJobName": "{% $prepared.transcription_job %}",
                "Media": {"MediaFileUri": "{% 's3://' & $prepared.bucket & '/' & $prepared.proxy_key %}"},
                "IdentifyLanguage": True,
                "OutputBucketName": "{% $prepared.bucket %}",
                "OutputKey": "{% $prepared.transcript_key %}",
            },
            iam_resources=jobs,
            outputs={},
        )
        wait = sfn.Wait.jsonata(steps, "WaitForTranscript", time=sfn.WaitTime.duration(POLL))
        get = tasks.CallAwsService.jsonata(
            steps,
            "GetTranscript",
            service="transcribe",
            action="getTranscriptionJob",
            parameters={"TranscriptionJobName": "{% $prepared.transcription_job %}"},
            iam_resources=jobs,
            # Only fields that are always there: an undefined JSONata value fails the state.
            outputs={"status": "{% $states.result.TranscriptionJob.TranscriptionJobStatus %}"},
        )
        done = sfn.Choice.jsonata(steps, "TranscriptDone")
        done.when(
            sfn.Condition.jsonata("{% $states.input.status = 'COMPLETED' %}"),
            sfn.Pass.jsonata(steps, "Transcript", outputs=[entry]),
        )
        done.when(sfn.Condition.jsonata("{% $states.input.status = 'FAILED' %}"), failed)
        done.otherwise(wait)
        for state in (start, get):
            state.add_catch(failed, outputs="{% $states.errorOutput %}")
        return start.next(wait).next(get).next(done)

    def _rekognition(
        self,
        steps: Construct,
        name: str,
        analyses: Sequence[Analysis],
        *,
        start: str,
        get: str,
        parameters: dict[str, Any],
    ) -> sfn.IChainable:
        failed = sfn.Pass.jsonata(
            steps,
            f"{name}Failed",
            outputs=[{"analysis": a.value, "error": "{% $string($states.input) %}"} for a in analyses],
        )
        started = tasks.CallAwsService.jsonata(
            steps,
            f"Start{name}",
            service="rekognition",
            action=start,
            parameters={**parameters, "ClientRequestToken": f"{{% $prepared.token & '-{name.lower()[:3]}' %}}"},
            iam_resources=["*"],
            outputs={"job_id": "{% $states.result.JobId %}"},
        )
        wait = sfn.Wait.jsonata(steps, f"WaitFor{name}", time=sfn.WaitTime.duration(POLL))
        polled = tasks.CallAwsService.jsonata(
            steps,
            f"Get{name}",
            service="rekognition",
            action=get,
            parameters={"JobId": "{% $states.input.job_id %}", "MaxResults": 1},
            iam_resources=["*"],
            outputs={"job_id": "{% $states.input.job_id %}", "status": "{% $states.result.JobStatus %}"},
        )
        done = sfn.Choice.jsonata(steps, f"{name}Done")
        done.when(
            sfn.Condition.jsonata("{% $states.input.status = 'SUCCEEDED' %}"),
            sfn.Pass.jsonata(
                steps,
                name,
                outputs=[{"analysis": a.value, "job_id": "{% $states.input.job_id %}"} for a in analyses],
            ),
        )
        done.when(sfn.Condition.jsonata("{% $states.input.status = 'FAILED' %}"), failed)
        done.otherwise(wait)
        for state in (started, polled):
            state.add_catch(failed, outputs="{% $states.errorOutput %}")
        return started.next(wait).next(polled).next(done)

    def _trigger(self, event_bus: events.IEventBus, removal_policy: RemovalPolicy) -> events.Rule:
        dead_letters = dead_letter_queue(self, "DeadLetters", removal_policy=removal_policy)
        rule = events.Rule(
            self,
            "Trigger",
            description=f"FAST Channels ({self.prefix}): analyse a title on its first encode",
            event_bus=event_bus,
            event_pattern=events.EventPattern(
                source=[event_source(self.prefix)],
                detail_type=[DETAIL_TYPES[AssetReady]],
                detail={"kind": [AssetKind.CONTENT.value]},
            ),
            targets=[
                events_targets.SfnStateMachine(
                    self.state_machine,
                    input=events.RuleTargetInput.from_object({"detail": events.EventField.from_path("$.detail")}),
                    dead_letter_queue=dead_letters,
                    retry_attempts=8,
                )
            ],
        )
        self.dead_letters = dead_letters
        return rule
