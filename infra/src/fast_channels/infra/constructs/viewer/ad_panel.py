"""The demo's "why these ads" panel API (``modules.viewer.ad_panel``): ``GET /sessions/{sid}/breaks`` on an HTTP
API, behind a JWT authorizer for the viewer pool's access tokens (functions/viewer/ad_panel.py)."""

from typing import Final, cast

from aws_cdk import ArnFormat, Duration, Stack
from aws_cdk import aws_apigatewayv2 as apigateway
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

from fast_channels.core.identity import names as identity_names
from fast_channels.core.pds.names import sidecar_log_group
from fast_channels.infra import nag
from fast_channels.infra.constructs.functions import FastFunction

PLAYBACK_SCOPE: Final = identity_names.PLAYBACK_SCOPE
"""The viewer pool's resource-server scope that its access tokens carry (and its ID tokens do not)."""
THROTTLE_RATE: Final = 5
"""Requests per second for the whole API: each runs a Logs Insights query, and the panel polls every 20 s."""
THROTTLE_BURST: Final = 10
CONCURRENCY: Final = 5
"""Reserved concurrency of the function: a bound on concurrent Logs Insights queries."""
WHY: Final = "The demo's ad panel"


class AdPanelApi(Construct):
    """The panel's function and HTTP API.

    The function reads the stage's PDS sidecar log group with Logs Insights, and may compute viewer ids with the
    identity module's KMS key (``kms:GenerateMac`` only) to check that a session is the caller's. API Gateway admits
    only access tokens of ``client_id`` from ``issuer`` with the scope ``fast/playback``. It answers CORS for any
    origin, as the playback API does: a bearer-token API with no cookies, which the viewer site's CSP names (the site
    cannot be named here: its CSP names the API, which would be a cycle).

    Args:
        prefix: the stage's prefix (the sidecar's log group is ``/aws/ecs/<prefix>-pds``).
        issuer: the viewer pool's issuer, ``https://cognito-idp.<region>.amazonaws.com/<pool id>``.
        client_id: the viewer pool's client whose tokens the viewer app holds.
        vid_key_arn: the identity module's viewer-id key.

    Attributes:
        handler: the function.
        api: the HTTP API.
        url: ``https://<api>.execute-api.<region>.amazonaws.com/`` (the ``$default`` stage).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        issuer: str,
        client_id: str,
        vid_key_arn: str,
        log_retention: logs.RetentionDays,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        log_group = sidecar_log_group(prefix)
        self.handler = FastFunction(
            self,
            "Function",
            handler="fast_channels.functions.viewer.ad_panel.handler",
            description="FAST Channels: the demo's ad panel (one viewer's PDS decision records per session)",
            environment={"DECISION_LOG_GROUP": log_group, "VID_KEY_ID": vid_key_arn},
            timeout=Duration.seconds(15),
            memory_size=256,
            log_retention=log_retention,
        )
        cast(
            "lambda_.CfnFunction", self.handler.function.node.default_child
        ).reserved_concurrent_executions = CONCURRENCY
        group_arn = stack.format_arn(
            service="logs", resource="log-group", resource_name=log_group, arn_format=ArnFormat.COLON_RESOURCE_NAME
        )
        self.handler.allow(
            ["logs:StartQuery"],
            [group_arn, f"{group_arn}:*"],
            why="Logs Insights queries of the PDS sidecar's log group (its ARN with and without the stream suffix)",
        )
        # Query results and stops have no resource-level permissions; a query id is only known to its starter.
        self.handler.allow(["logs:GetQueryResults", "logs:StopQuery"], ["*"])
        self.handler.allow(
            ["kms:GenerateMac"], [vid_key_arn], conditions={"StringEquals": {"kms:MacAlgorithm": "HMAC_SHA_256"}}
        )
        nag.acknowledge(self.handler.function, {nag.LAMBDA_DLQ: f"{WHY}; invoked synchronously by API Gateway"})

        self.api = apigateway.HttpApi(
            self,
            "Api",
            api_name=f"{prefix}-viewer-ad-panel",
            description="FAST Channels: the demo's ad panel (GET /sessions/{sid}/breaks, viewer access tokens)",
            create_default_stage=False,
            cors_preflight=apigateway.CorsPreflightOptions(
                allow_origins=["*"],
                allow_methods=[apigateway.CorsHttpMethod.GET],
                allow_headers=["Authorization"],
                max_age=Duration.hours(1),
            ),
        )
        self.api.add_routes(
            path="/sessions/{sid}/breaks",
            methods=[apigateway.HttpMethod.GET],
            integration=integrations.HttpLambdaIntegration("Breaks", self.handler.function),
            authorizer=authorizers.HttpJwtAuthorizer("Viewers", issuer, jwt_audience=[client_id]),
            authorization_scopes=[PLAYBACK_SCOPE],
        )
        stage = apigateway.HttpStage(
            self,
            "Stage",
            http_api=self.api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigateway.ThrottleSettings(rate_limit=THROTTLE_RATE, burst_limit=THROTTLE_BURST),
        )
        access = f"{WHY}; the function logs every answer"
        nag.acknowledge(stage, {"AwsSolutions-APIG1": access, "Serverless-APIGWAccessLogging": access})
        self.url = f"{self.api.api_endpoint}/"
