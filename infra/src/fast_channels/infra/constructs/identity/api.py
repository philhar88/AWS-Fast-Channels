"""The identity API: ``POST /playback`` (the ticket mint) and ``POST /redeem`` (the PRE_SESSION Function's binding).

API Gateway REST, regional, one stage ``v1`` throttled per ``modules.identity``.

- ``POST /playback``: the viewer pool's Cognito authorizer with the authorization scope ``fast/playback``, so only an
  access token carrying it reaches the mint Lambda (an ID token never carries a scope). CORS allows any origin: it is
  a bearer-token API without cookies. The 4xx and 5xx gateway responses carry the CORS header too, so a browser sees
  the error.
- ``POST /redeem``: no Lambda. An AWS integration runs DynamoDB ``UpdateItem`` on ``T#<t>`` with the redeem condition
  (``core.identity.ticket.REDEEM_CONDITION``), ``:now`` being API Gateway's request time. A request validator holds
  the body to ``{t, sid, pc}`` of the expected shapes first, so nothing else reaches the template. 200
  ``{"ok":true}`` binds; a failed condition (a replay, an expired, unknown or other configuration's ticket) is 409,
  and the session stays anonymous. MediaTailor's PRE_SESSION Function calls it without credentials: binding a ticket
  needs the ticket itself (128 random bits), and the stage's throttling caps any guessing.

Access logs go to the API's own log group, without the caller's address, headers or body.
"""

import json
from typing import TYPE_CHECKING, Final

from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from constructs import Construct

from fast_channels.core.identity.names import PLAYBACK_SCOPE, api_name
from fast_channels.core.identity.ticket import REDEEM_CONDITION, REDEEM_UPDATE
from fast_channels.infra import nag

if TYPE_CHECKING:
    from aws_cdk import RemovalPolicy
    from aws_cdk import aws_cognito as cognito
    from aws_cdk import aws_dynamodb as dynamodb
    from aws_cdk import aws_lambda as lambda_

STAGE: Final = "v1"
CORS_ORIGIN: Final = "'*'"
REDEEM_SCHEMA: Final = apigw.JsonSchema(
    type=apigw.JsonSchemaType.OBJECT,
    required=["t", "sid", "pc"],
    additional_properties=False,
    properties={
        "t": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, pattern="^[A-Za-z0-9_-]{22}$"),
        "sid": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, pattern="^[A-Za-z0-9-]{1,64}$"),
        "pc": apigw.JsonSchema(type=apigw.JsonSchemaType.STRING, pattern="^[A-Za-z0-9_-]{1,64}$"),
    },
)
"""The redeem body: the ticket as the edge keeps it, MediaTailor's ``session.uuid`` and the sibling's name."""
ACCESS_LOG: Final = {
    "requestId": "$context.requestId",
    "time": "$context.requestTimeEpoch",
    "method": "$context.httpMethod",
    "resource": "$context.resourcePath",
    "status": "$context.status",
    "latencyMs": "$context.responseLatency",
    "integrationLatencyMs": "$context.integration.latency",
    "error": "$context.error.messageString",
    "authorizerError": "$context.authorizer.error",
}
"""What an access-log line holds: no address, header, body or token claim."""


def redeem_template(table: str) -> str:
    """The VTL request template of the redeem: ``UpdateItem`` with the redeem condition, ``:now`` the request time."""
    return "#set($b = $input.path('$'))\n" + json.dumps(
        {
            "TableName": table,
            "Key": {"pk": {"S": "T#$util.escapeJavaScript($b.t)"}},
            "UpdateExpression": REDEEM_UPDATE,
            "ConditionExpression": REDEEM_CONDITION,
            "ExpressionAttributeValues": {
                ":sid": {"S": "$util.escapeJavaScript($b.sid)"},
                ":pc": {"S": "$util.escapeJavaScript($b.pc)"},
                ":now": {"N": "$context.requestTimeEpoch"},
            },
        },
        indent=2,
    )


class IdentityApi(Construct):
    """The REST API (module docstring).

    Args:
        prefix: deployment prefix (``<prefix>-identity``).
        user_pool: the viewer pool (the authorizer).
        mint: the ``POST /playback`` function.
        table: the ticket table (the redeem's ``UpdateItem``).
        rate_limit, burst_limit: the stage's throttling.
        log_retention, removal_policy: of the access-log group.

    Attributes:
        api: the REST API; ``url`` its stage URL (``https://…/v1/``).
        playback_url, redeem_url: the two methods' URLs.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        user_pool: cognito.IUserPool,
        mint: lambda_.IFunction,
        table: dynamodb.ITableV2,
        rate_limit: int,
        burst_limit: int,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy,
    ) -> None:
        super().__init__(scope, id)
        access_logs = logs.LogGroup(self, "AccessLogs", retention=log_retention, removal_policy=removal_policy)
        self.api = apigw.RestApi(
            self,
            "Api",
            rest_api_name=api_name(prefix),
            description=f"FAST Channels ({prefix}): playback tickets for signed-in viewers",
            endpoint_types=[apigw.EndpointType.REGIONAL],
            # A REST API logs through the account's API Gateway CloudWatch role (one per account and Region). A stack
            # must not take that setting over, so access logs need it set already (docs/identity.md#logs).
            cloud_watch_role=False,
            deploy_options=apigw.StageOptions(
                stage_name=STAGE,
                throttling_rate_limit=rate_limit,
                throttling_burst_limit=burst_limit,
                tracing_enabled=True,
                access_log_destination=apigw.LogGroupLogDestination(access_logs),
                access_log_format=apigw.AccessLogFormat.custom(json.dumps(ACCESS_LOG)),
            ),
        )
        for name, kind in (
            ("Default4xx", apigw.ResponseType.DEFAULT_4_XX),
            ("Default5xx", apigw.ResponseType.DEFAULT_5_XX),
        ):
            self.api.add_gateway_response(
                name, type=kind, response_headers={"Access-Control-Allow-Origin": CORS_ORIGIN}
            )
        authorizer = apigw.CognitoUserPoolsAuthorizer(
            self, "Viewers", cognito_user_pools=[user_pool], authorizer_name=f"{prefix}-viewers"
        )
        playback = self.api.root.add_resource(
            "playback",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["POST", "OPTIONS"],
                allow_headers=["Authorization", "Content-Type"],
                max_age=None,
            ),
        )
        playback.add_method(
            "POST",
            apigw.LambdaIntegration(mint),
            authorizer=authorizer,
            authorization_type=apigw.AuthorizationType.COGNITO,
            authorization_scopes=[PLAYBACK_SCOPE],
        )
        self._redeem(table)
        self.playback_url = f"{self.api.url}playback"
        self.redeem_url = f"{self.api.url}redeem"
        nag.acknowledge(
            self.api,
            {
                "AwsSolutions-APIG2": "POST /playback validates its body in the mint; POST /redeem has a request "
                "validator",
                "AwsSolutions-APIG3": "Stage throttling bounds the API; a web ACL would bill every play",
                "AwsSolutions-APIG4": "POST /redeem is MediaTailor's PRE_SESSION call, which has no credential to "
                "send: binding needs the ticket itself; the CORS preflights carry no authorization by design",
                "AwsSolutions-APIG6": "Execution logging needs the account-wide API Gateway CloudWatch role; the stage "
                "writes access logs",
                "AwsSolutions-COG4": "POST /redeem is called by MediaTailor, not a viewer (see APIG4)",
            },
        )

    def _redeem(self, table: dynamodb.ITableV2) -> None:
        role = iam.Role(self, "RedeemRole", assumed_by=iam.ServicePrincipal("apigateway.amazonaws.com"))
        role.add_to_policy(iam.PolicyStatement(actions=["dynamodb:UpdateItem"], resources=[table.table_arn]))
        model = self.api.add_model(
            "RedeemBody", content_type="application/json", model_name="RedeemBody", schema=REDEEM_SCHEMA
        )
        validator = self.api.add_request_validator("RedeemBodyValidator", validate_request_body=True)
        json_type = "application/json"
        integration = apigw.AwsIntegration(
            service="dynamodb",
            action="UpdateItem",
            options=apigw.IntegrationOptions(
                credentials_role=role,
                passthrough_behavior=apigw.PassthroughBehavior.NEVER,
                request_templates={json_type: redeem_template(table.table_name)},
                integration_responses=[
                    apigw.IntegrationResponse(status_code="200", response_templates={json_type: '{"ok": true}'}),
                    apigw.IntegrationResponse(
                        status_code="409", selection_pattern="4\\d{2}", response_templates={json_type: '{"ok": false}'}
                    ),
                    apigw.IntegrationResponse(
                        status_code="502", selection_pattern="5\\d{2}", response_templates={json_type: '{"ok": false}'}
                    ),
                ],
            ),
        )
        self.api.root.add_resource("redeem").add_method(
            "POST",
            integration,
            request_validator=validator,
            request_models={json_type: model},
            method_responses=[apigw.MethodResponse(status_code=code) for code in ("200", "409", "502")],
        )
