"""Captioners: state machines that honour the captioner contract (``functions.ingest.captions``).

The ingest pipeline runs its captioner (``IngestPipeline(captioner=...)``) when an asset's spoken language has no
captions: no file came with it and the caption scan found none in the source. Any state machine with the contract's
input and output plugs in, a third-party captioning service included.

* :class:`InferenceCaptioner` - AWS Elemental Inference smart subtitles, fed with a MediaConvert proxy of the file.
"""

from typing import Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_mediaconvert as mediaconvert
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct

from fast_channels.core.ingest import CAPTIONS_PREFIX, WORK_PREFIX
from fast_channels.core.tags import TAG_APP, Owner, resource_tags
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.grants import allow

CAPTIONER_TIMEOUT: Final = Duration.hours(6)
PROXY_TIMEOUT: Final = Duration.hours(2)
RELAY_TIMEOUT: Final = Duration.minutes(15)
"""One chunk (``core.inference.CHUNK_SEGMENTS``) relays in about 7.5 minutes, then settles and is read back."""
HANDLER: Final = "fast_channels.functions.captioners.inference.handler"
INVOKED: Final = "Invoked synchronously by Step Functions, which retries and catches its failures"


class InferenceCaptioner(Construct):
    """Generates captions with AWS Elemental Inference smart subtitles (``functions.captioners.inference``).

    A Standard JSONata state machine (logs ``/aws/vendedlogs/states/<prefix>-captioner``, X-Ray): ``Proxy`` (the
    proxy job's settings) -> ``ProxyJob`` (MediaConvert, run-a-job, on the captioner's own queue and role) -> ``Plan``
    (chunks) -> ``Relay`` (a Map of one fresh feed per chunk, ``max_feeds`` at a time; a chunk that finds the account's
    active feeds in use is retried with backoff) -> ``Stitch`` (one IMSC1 TTML file at the contract's
    ``output_uri``). A failure runs ``Cleanup``, which deletes the execution's feeds, and fails the execution.

    Feeds are tagged with the deployment (``fast:app``); the function may use only such feeds.

    Args:
        prefix: deployment prefix (names, tags).
        ingest_bucket: the mezzanines it reads, ``work/`` for the proxy and chunk words, ``captions/`` for output.
        max_feeds: chunks relayed at once; the account's ``Active feeds per account`` quota (2 by default) is shared
            with every other use of Elemental Inference.
        dictionary_id: an Elemental Inference custom dictionary every feed uses, if any.
        log_retention / removal_policy: of the log groups.

    Attributes:
        engine: ``elemental-inference``.
        state_machine, function, queue, role, log_group.
    """

    engine: Final = "elemental-inference"

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        ingest_bucket: s3.IBucket,
        max_feeds: int = 2,
        dictionary_id: str | None = None,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.prefix = prefix
        self.ingest_bucket = ingest_bucket
        self.queue = mediaconvert.CfnQueue(
            self,
            "Queue",
            name=f"{prefix}-captioner",
            description=f"FAST Channels ({prefix}): Elemental Inference proxies of titles without captions",
            pricing_plan="ON_DEMAND",
            concurrent_jobs=5,
            status="ACTIVE",
            tags=resource_tags(prefix, Owner.IAC),
        )
        self.role = iam.Role(
            self,
            "MediaConvertRole",
            description=f"FAST Channels ({prefix}): MediaConvert writes Elemental Inference proxies",
            assumed_by=iam.ServicePrincipal(
                "mediaconvert.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": Stack.of(self).account},
                    "ArnEquals": {"aws:SourceArn": self.queue.attr_arn},
                },
            ),
        )
        allow(self.role, ["s3:GetObject"], [ingest_bucket.arn_for_objects("*")], why="Mezzanines may have any key")
        allow(
            self.role,
            ["s3:PutObject"],
            [ingest_bucket.arn_for_objects(f"{WORK_PREFIX}*")],
            why="Proxies go to work/<asset>/<correlation id>/inference/",
        )

        environment = {"FAST_PREFIX": prefix, "INGEST_BUCKET": ingest_bucket.bucket_name}
        if dictionary_id:
            environment["INFERENCE_DICTIONARY"] = dictionary_id
        self.function = FastFunction(
            self,
            "Function",
            handler=HANDLER,
            description="Captioner: Elemental Inference proxy, chunk relays, stitched captions",
            environment=environment,
            timeout=RELAY_TIMEOUT,
            memory_size=1024,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        nag.acknowledge(self.function, {nag.LAMBDA_DLQ: INVOKED})
        self._grant_function()

        self.log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/vendedlogs/states/{prefix}-captioner",
            retention=log_retention,
            removal_policy=removal_policy,
        )
        self.state_machine = sfn.StateMachine(
            self,
            "StateMachine",
            state_machine_name=f"{prefix}-captioner",
            comment="Generate an asset's captions with AWS Elemental Inference (the ingest pipeline's captioner)",
            query_language=sfn.QueryLanguage.JSONATA,
            definition_body=sfn.DefinitionBody.from_chainable(self._definition(max_feeds)),
            logs=sfn.LogOptions(destination=self.log_group, level=sfn.LogLevel.ALL, include_execution_data=True),
            tracing_enabled=True,
            timeout=CAPTIONER_TIMEOUT,
        )
        nag.acknowledge_references(
            self.state_machine.role,
            {
                nag.wildcard_resource(): "MediaConvert CreateJob/GetJob/CancelJob (job ARNs exist only after "
                "creation) and X-Ray, which has no resource-level permissions",
                nag.wildcard_resource(f"{self.function.function.function_arn}:*"): "Invoke the steps and versions",
            },
        )

    def _grant_function(self) -> None:
        fn, bucket = self.function, self.ingest_bucket
        fn.allow(
            ["s3:GetObject", "s3:PutObject"],
            [bucket.arn_for_objects(f"{WORK_PREFIX}*")],
            why="Proxies and chunk words of any asset",
        )
        fn.allow(["s3:PutObject"], [bucket.arn_for_objects(f"{CAPTIONS_PREFIX}*")], why="Captions of any asset")
        # ListBucket turns a missing segment into a 404 instead of a 403.
        fn.allow(["s3:ListBucket"], [bucket.bucket_arn])
        feeds = arn(self, "elemental-inference", "feed", "*")
        why = "Feeds are created per chunk at run time; only feeds tagged with this deployment"
        fn.allow(
            ["elemental-inference:CreateFeed", "elemental-inference:TagResource"],
            [feeds],
            why=why,
            conditions={"StringEquals": {f"aws:RequestTag/{TAG_APP}": self.prefix}},
        )
        fn.allow(
            [
                "elemental-inference:AssociateFeed",
                "elemental-inference:DeleteFeed",
                "elemental-inference:GetFeed",
                "elemental-inference:PutMedia",
                "elemental-inference:GetMetadata",
            ],
            [feeds],
            why=why,
            conditions={"StringEquals": {f"aws:ResourceTag/{TAG_APP}": self.prefix}},
        )
        fn.allow(["elemental-inference:ListFeeds"], ["*"])  # no resource-level permissions

    def _definition(self, max_feeds: int) -> sfn.IChainable:
        steps = Construct(self, "Steps")

        def step(id: str, name: str, *, assign: dict[str, str] | None = None, **fields: str) -> tasks.LambdaInvoke:
            payload = {
                "step": name,
                "request": "{% $states.context.Execution.Input %}",
                "execution": "{% $states.context.Execution.Name %}",
                **fields,
            }
            return tasks.LambdaInvoke.jsonata(
                steps,
                id,
                lambda_function=self.function.function,
                payload=sfn.TaskInput.from_object(payload),
                assign=assign,
                outputs="{% $states.result.Payload %}",
            )

        proxy = step("Proxy", "proxy", assign={"prepared": "{% $states.result.Payload %}"})
        proxy_job = tasks.MediaConvertCreateJob.jsonata(
            steps,
            "ProxyJob",
            create_job_request={
                "Role": self.role.role_arn,
                "Queue": self.queue.attr_arn,
                "Settings": "{% $prepared.job.Settings %}",
                "UserMetadata": "{% $prepared.job.UserMetadata %}",
                "ClientRequestToken": "{% $prepared.job.ClientRequestToken %}",
            },
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            task_timeout=sfn.Timeout.duration(PROXY_TIMEOUT),
        )
        proxy_job.add_retry(
            errors=["MediaConvert.TooManyRequestsException", "MediaConvert.InternalServerErrorException"],
            interval=Duration.seconds(5),
            max_attempts=4,
            backoff_rate=2,
            jitter_strategy=sfn.JitterType.FULL,
        )
        plan = step("Plan", "plan", proxy="{% $prepared %}", assign={"chunks": "{% $states.result.Payload %}"})
        relay = step("RelayChunk", "relay", proxy="{% $prepared %}", chunk="{% $states.input %}")
        relay.add_retry(
            errors=["FeedQuotaExceededError"],
            interval=Duration.seconds(30),
            max_attempts=40,
            backoff_rate=1.5,
            max_delay=Duration.minutes(5),
            jitter_strategy=sfn.JitterType.FULL,
        )
        relays = sfn.Map.jsonata(
            steps,
            "Relay",
            items=sfn.ProvideItems.jsonata("{% $chunks %}"),
            max_concurrency=max_feeds,
            assign={"relayed": "{% $states.result %}"},
        )
        relays.item_processor(relay)
        stitch = step("Stitch", "stitch", chunks="{% $relayed %}")
        cleanup = step("Cleanup", "cleanup", error="{% $states.input %}", assign={"error": "{% $states.input %}"})
        failed = sfn.Fail.jsonata(steps, "CaptioningFailed", error="{% $error.Error %}", cause="{% $error.Cause %}")
        cleanup.next(failed)
        for task in (proxy, proxy_job, plan, relays, stitch):
            task.add_catch(cleanup, outputs="{% $states.errorOutput %}")
        return (
            proxy.next(proxy_job)
            .next(plan)
            .next(relays)
            .next(stitch)
            .next(sfn.Succeed.jsonata(steps, "Captioned", outputs="{% $states.input %}"))
        )
