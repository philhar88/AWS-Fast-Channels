"""The rail recommender (``modules.recs``; ADR-0027): ``GET /rail``, the rail builder and the hourly cohort rails."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigateway
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_destinations as destinations
from aws_cdk import aws_scheduler as scheduler
from aws_cdk import aws_scheduler_targets as scheduler_targets
from constructs import Construct

from fast_channels.core.identity import names as identity_names
from fast_channels.core.model_registry import task_parameter
from fast_channels.core.recs import TASK
from fast_channels.core.recs.serve import COHORT_PREFIX
from fast_channels.functions.epg import EPG_PREFIX
from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.alerts import dead_letter_alarm
from fast_channels.infra.constructs.functions import FastFunction
from fast_channels.infra.constructs.models.caps import counter_table_name, grant_counting
from fast_channels.infra.constructs.models.profiles import foundation_model, task_invoke_statements
from fast_channels.infra.constructs.models.registry import entry_arn, render
from fast_channels.infra.constructs.queues import dead_letter_queue

if TYPE_CHECKING:
    from aws_cdk import aws_logs as logs
    from aws_cdk import aws_s3 as s3
    from aws_cdk import aws_sns as sns

    from fast_channels.core.model_registry import TaskSettings
    from fast_channels.infra.exports import TaskProfile

API_HANDLER: Final = "fast_channels.functions.recs.api.handler"
BUILDER_HANDLER: Final = "fast_channels.functions.recs.builder.handler"
COHORT_SCHEDULE: Final = "rate(1 hour)"
THROTTLE_RATE: Final = 50
THROTTLE_BURST: Final = 100
API_CONCURRENCY: Final = 10
"""Reserved concurrency of ``GET /rail`` (DESIGN §15): a burst of viewers cannot starve the stage's other functions."""
BUILDER_TIMEOUT: Final = Duration.seconds(90)
"""Two model attempts of at most 30 s each, and the reads around them."""
_SSM_READ: Final = ("ssm:GetParameter",)


@dataclass(frozen=True, slots=True)
class IdentityValues:
    """What the recommender reads of the identity module (its SSM parameters' values, resolved at deployment)."""

    issuer: str
    client_ids: tuple[str, ...]
    """Clients whose access tokens may call ``GET /rail``: the viewer app's (the e2e tests sign in with it too; an
    authorizer's audiences must be distinct)."""
    vid_key_arn: str


class RailRecommender(Construct):
    """The recommender's table, functions, API and schedule.

    - ``<prefix>-recs`` (DynamoDB, on demand, TTL ``expires_at``): viewers' rail state and the built rails.
    - **The rail API** (``GET /rail`` on an HTTP API, viewer access tokens with ``fast/playback``): reads the
      guides, the built rails, the cohort rails and the viewer's consent row (``C#<vid>`` only), computes the
      ``vid`` (``kms:GenerateMac`` only), and starts builds; it never invokes a model.
    - **The rail builder**: the only caller of the ``recommender`` task's application inference profile (its model
      only through that profile). It reads the task's registry entry, counts the task's spend (``COUNTER#recommender#``
      items only), reads the guides and the catalog, writes the built rails and the cohort rails
      (``plan/rail/`` of the media bucket). Asynchronous invocations are not retried: a failure is recorded with its
      reason, and an unhandled one is dead-lettered and alarmed.
    - **An hourly schedule** builds the cohort rails.

    Args:
        prefix: deployment prefix.
        channels: the channels rails recommend, in the viewer's order.
        child_directed: channels whose titles only a kids lineup shows.
        cohorts: ``<device>-<language>`` of each cohort rail.
        settings: the ``recommender`` registry entry (``{geo}`` resolved); profile: its application inference profile.
        identity: the identity module's issuer, clients and viewer-id key.
        media_bucket: the guides and the cohort rails; catalog_table: the reference catalog.
        alert_topic: told when the task's cap is reached and when a build is dead-lettered.
        appconfig: the ops module's AppConfig application and environment ids (the ``recs.strategy`` flag), or ``None``
            without ops.

    Attributes:
        table, api_function, builder, api, url (``https://…/`` of the ``$default`` stage), rail_url, cohorts, alarm.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        channels: Sequence[str],
        child_directed: Sequence[str],
        cohorts: Sequence[str],
        holdout_percent: int,
        refresh_hours: int,
        settings: TaskSettings,
        profile: TaskProfile,
        identity: IdentityValues,
        media_bucket: s3.IBucket,
        catalog_table: dynamodb.ITableV2,
        alert_topic: sns.ITopic,
        appconfig: tuple[str, str] | None,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.table = dynamodb.TableV2(
            self,
            "Table",
            table_name=f"{prefix}-recs",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            time_to_live_attribute="expires_at",
            removal_policy=removal_policy,
        )
        nag.acknowledge(
            self.table,
            {"AwsSolutions-DDB3": "Rails are rebuilt from the guides and the catalog; a restore has no value"},
        )
        common = {
            "FAST_PREFIX": prefix,
            "RECS_TABLE": self.table.table_name,
            "MEDIA_BUCKET": media_bucket.bucket_name,
            "RECS_CHANNELS": ",".join(channels),
            "RECS_CHILD_DIRECTED": ",".join(child_directed),
            "RECS_COHORTS": ",".join(cohorts),
            "RECS_HOLDOUT_PERCENT": str(holdout_percent),
            "RECS_REFRESH_HOURS": str(refresh_hours),
        }

        self.dead_letters = dead_letter_queue(self, "DeadLetters", removal_policy=removal_policy)
        self.builder = FastFunction(
            self,
            "Builder",
            handler=BUILDER_HANDLER,
            description="Recs: build a viewer's model rail or the hourly cohort rails (the recommender task)",
            environment={
                **common,
                "CATALOG_TABLE": catalog_table.table_name,
                "MODEL_TASK_PARAMETER": task_parameter(prefix, TASK),
                "MODEL_TASK_DEFAULT": render(settings, profile.arn),
                "MODEL_COUNTER_TABLE": counter_table_name(prefix),
                "ALERT_TOPIC_ARN": alert_topic.topic_arn,
            },
            timeout=BUILDER_TIMEOUT,
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        build = self.builder.function
        build.configure_async_invoke(
            on_failure=destinations.SqsDestination(self.dead_letters), retry_attempts=0, max_event_age=Duration.hours(1)
        )
        nag.acknowledge(self.builder, {nag.LAMBDA_DLQ: "Failed asynchronous invocations go to the DeadLetters queue"})
        for statement in task_invoke_statements(profile, partition=stack.partition):
            build.add_to_role_policy(statement)
        family = foundation_model(profile.model)
        nag.acknowledge_references(
            self.builder,
            {
                nag.wildcard_resource(f"arn:{stack.partition}:bedrock:*::foundation-model/{family}"): (
                    f"Application inference profile of model task {TASK}: routes to the Regions of the profile it "
                    "copies; allowed only through the task's profile (bedrock:InferenceProfileArn)"
                )
            },
        )
        self.builder.allow(list(_SSM_READ), [entry_arn(self, prefix=prefix, task=TASK)])
        grant_counting(build, self, prefix=prefix, task=TASK)
        self.builder.allow(["sns:Publish"], [alert_topic.topic_arn])
        self.builder.allow(["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"], [self.table.table_arn])
        self.builder.allow(["dynamodb:BatchGetItem", "dynamodb:GetItem"], [catalog_table.table_arn])
        self.builder.allow(
            ["s3:GetObject"], [media_bucket.arn_for_objects(f"{EPG_PREFIX}*")], why="Every channel's guide"
        )
        self.builder.allow(
            ["s3:PutObject"], [media_bucket.arn_for_objects(f"{COHORT_PREFIX}*")], why="One cohort rail per cohort"
        )

        self.api_function = FastFunction(
            self,
            "Api",
            handler=API_HANDLER,
            description="Recs: GET /rail, a signed-in viewer's rail (no model call)",
            environment={
                **common,
                "IDENTITY_TABLE": identity_names.table_name(prefix),
                "VID_KEY": identity.vid_key_arn,
                "RECS_BUILDER_FUNCTION": build.function_name,
                **(
                    {"APPCONFIG_APPLICATION": f"{prefix}-fast", "APPCONFIG_ENVIRONMENT": prefix}
                    if appconfig is not None
                    else {}
                ),
            },
            timeout=Duration.seconds(10),
            log_retention=log_retention,
            removal_policy=removal_policy,
        )
        api_fn = self.api_function.function
        cast("lambda_.CfnFunction", api_fn.node.default_child).reserved_concurrent_executions = API_CONCURRENCY
        nag.acknowledge(self.api_function, {nag.LAMBDA_DLQ: "Invoked synchronously by API Gateway"})
        self.api_function.allow(
            ["kms:GenerateMac"],
            [identity.vid_key_arn],
            conditions={"StringEquals": {"kms:MacAlgorithm": "HMAC_SHA_256"}},
        )
        self.api_function.allow(
            ["dynamodb:GetItem"],
            [arn(self, "dynamodb", "table", identity_names.table_name(prefix))],
            conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["C#*"]}},
        )
        self.api_function.allow(["dynamodb:GetItem", "dynamodb:UpdateItem"], [self.table.table_arn])
        self.api_function.allow(
            ["s3:GetObject"],
            [media_bucket.arn_for_objects(f"{EPG_PREFIX}*"), media_bucket.arn_for_objects(f"{COHORT_PREFIX}*")],
            why="Every channel's guide and every cohort rail",
        )
        self.api_function.allow(["lambda:InvokeFunction"], [build.function_arn])
        if appconfig is not None:
            self.api_function.allow(
                ["appconfig:StartConfigurationSession", "appconfig:GetLatestConfiguration"],
                [
                    arn(
                        self,
                        "appconfig",
                        "application",
                        f"{appconfig[0]}/environment/{appconfig[1]}/configuration/*",
                    )
                ],
                why="The flags profile of the stage's AppConfig application (recs.strategy)",
            )

        self.api = apigateway.HttpApi(
            self,
            "HttpApi",
            api_name=f"{prefix}-recs",
            description="FAST Channels: GET /rail, a signed-in viewer's recommended rail (viewer access tokens)",
            create_default_stage=False,
            cors_preflight=apigateway.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigateway.CorsHttpMethod.GET],
                allow_headers=["Authorization"],
                max_age=Duration.hours(1),
            ),
        )
        self.api.add_routes(
            path="/rail",
            methods=[apigateway.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("Rail", api_fn),
            authorizer=authorizers.HttpJwtAuthorizer(
                "Viewers", identity.issuer, jwt_audience=list(identity.client_ids)
            ),
            authorization_scopes=[identity_names.PLAYBACK_SCOPE],
        )
        stage = apigateway.HttpStage(
            self,
            "Stage",
            http_api=self.api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigateway.ThrottleSettings(rate_limit=THROTTLE_RATE, burst_limit=THROTTLE_BURST),
        )
        access = "The rail function logs every answer (strategy, reason, tiles) and emits Rails metrics"
        nag.acknowledge(stage, {"AwsSolutions-APIG1": access, "Serverless-APIGWAccessLogging": access})
        self.url = f"{self.api.api_endpoint}/"
        self.rail_url = f"{self.api.api_endpoint}/rail"

        group = scheduler.ScheduleGroup(
            self, "Group", schedule_group_name=f"{prefix}-recs", removal_policy=RemovalPolicy.DESTROY
        )
        role = iam.Role(
            self,
            "ScheduleRole",
            description="EventBridge Scheduler: invoke the hourly cohort rails and dead-letter failed deliveries",
            assumed_by=iam.ServicePrincipal(
                "scheduler.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnEquals": {"aws:SourceArn": group.schedule_group_arn},
                },
            ),
        )
        self.cohorts = scheduler.Schedule(
            self,
            "Cohorts",
            schedule=scheduler.ScheduleExpression.expression(COHORT_SCHEDULE),
            schedule_group=group,
            description="Recs: build each cohort's rail for the current daypart (plan/rail/<cohort>.json)",
            target=scheduler_targets.LambdaInvoke(
                build,
                input=scheduler.ScheduleTargetInput.from_object({"cohorts": True}),
                retry_attempts=0,
                dead_letter_queue=self.dead_letters,
                role=role,
            ),
        )
        nag.acknowledge_references(
            role,
            {
                nag.wildcard_resource(
                    f"{build.function_arn}:*"
                ): "Scheduler L2 grant: invoke the function and its versions"
            },
        )
        self.alarm = dead_letter_alarm(
            self,
            "DeadLettersAlarm",
            queue=self.dead_letters,
            name=f"{prefix}-recs-builder-dead-letters",
            description="A rail build failed unhandled; viewers get the rule-based rail until a build succeeds",
            topic=alert_topic,
        )
