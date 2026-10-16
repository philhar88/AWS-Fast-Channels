"""Ladder kits: the filler slate, break slates, house ads and VAST pod every channel of a ladder needs (SPEC §5.2).

* :class:`LadderProvisioner` - the state machine that encodes a ladder's kit as IngestPipeline child executions.
* :class:`LadderKit` - a deploy-time custom resource that provisions a built-in ladder's kit.
"""

import hashlib
from collections.abc import Mapping
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Final, cast

from aws_cdk import CustomResource, Duration, Fn
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from aws_cdk import custom_resources as cr
from constructs import Construct, IConstruct

from fast_channels.core.timeline import BREAK_SLATE_GOPS, KIT_SPEC
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.bundling import REPO_ROOT
from fast_channels.infra.constructs._provider import provider
from fast_channels.infra.constructs.encoding.pipeline import FUNCTIONS, STEP_FUNCTIONS_INVOKED, IngestPipeline
from fast_channels.infra.constructs.functions import FastFunction

if TYPE_CHECKING:
    from fast_channels.infra.constructs.encoding.ladder import EncodingLadder

BRAND_DIR: Final = REPO_ROOT / "assets" / "brand"
"""Brand stills of every kit (rendered by ``scripts/render_brand_assets.py``)."""
BRAND_PREFIX: Final = "brand/"
"""Ingest-bucket prefix of the deployed brand stills: ``brand/<digest>/<file>.png``."""
_KIT_HANDLERS: Final = "fast_channels.functions.custom_resources.ladder_kit"


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
        log_group: the state machine's log group.
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
            handler=f"{FUNCTIONS}.kit.handler",
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
        nag.acknowledge(self.kit, {nag.LAMBDA_DLQ: STEP_FUNCTIONS_INVOKED})
        self.kit.allow(["dynamodb:UpdateItem"], [pipeline.library.table_arn])
        self.kit.allow(
            ["s3:PutObject"], [pipeline.media_bucket.arn_for_objects("ads/*")], why="VAST pods of every ladder"
        )
        pipeline.event_bus.grant_put_events_to(self.kit.function)

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
        nag.acknowledge_references(
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
        nag.acknowledge_references(
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
        nag.acknowledge_references(
            cast("IConstruct", is_complete.function.role),
            {nag.wildcard_resource(f"{_executions(self.state_machine)}:*"): "Poll provisioning executions"},
        )
        return provider(
            self,
            "KitProvider",
            on_event=on_event,
            is_complete=is_complete,
            waiter_log_group_name=f"/aws/vendedlogs/states/{pipeline.prefix}-ladder-kit-waiter",
            waiter_logs_id="KitWaiterLogs",
            total_timeout=Duration.hours(1),
            log_retention=pipeline.log_retention,
            removal_policy=pipeline.removal_policy,
        )

    def _kit_handler(self, id: str, name: str, description: str) -> FastFunction:
        handler = FastFunction(
            self,
            id,
            handler=f"{_KIT_HANDLERS}.{name}",
            description=description,
            environment={"LIBRARY_TABLE": self.pipeline.library.table_name},
            log_retention=self.pipeline.log_retention,
            removal_policy=self.pipeline.removal_policy,
        )
        handler.allow(["dynamodb:GetItem"], [self.pipeline.library.table_arn])
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
                "KitSpec": KIT_SPEC,
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


def brand_digest(directory: Path = BRAND_DIR) -> str:
    """Hash of the brand stills (file names and bytes), 10 hex characters."""
    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.png")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _executions(state_machine: sfn.IStateMachine) -> str:
    """``arn:...:execution:<state machine name>``, as the CDK Step Functions grants write it."""
    return arn(
        state_machine, "states", "execution", Fn.select(6, Fn.split(":", state_machine.state_machine_arn)), colon=True
    )
