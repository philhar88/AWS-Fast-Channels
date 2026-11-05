"""``Agents``: the AWS reference buyer and governance agents on AgentCore Runtime, from pinned images.

``OPTIMIZATION(aws): W-10 items 3 and 4``: the guidance's own deployer (``deploy_all.py``) cannot run inside a
stage's names and grants, and the guidance does not publish its agents as images, so the stage builds the pinned,
unmodified images (``SellingImageBuild``) and deploys them here, as P0-13 did:

- **governance** (MCP, port 8000): its plans, checks and spend aggregate in ``<prefix>-governance-state``, and its
  ``governance_context`` tokens signed with an ES256 KMS key. Inbound is a Cognito JWT (its client-credentials
  client), because the buyer reaches it with OAuth 2.0 client credentials and the agent reads the caller from the
  token. Nothing verifies the token at the seller yet (W-8 A2).
- **buyer** (A2A, port 9000): SigV4 inbound, so only principals of the account invoke it. It reaches the seller over
  A2A 1.0 with the seller's bearer token, and the governance agent over MCP. ``OPTIMIZATION(aws): W-10 item 1``: its
  ``create_media_buy`` refuses without an approved ``check_governance``, so the governance agent is deployed even
  where a stage needs no governance. ``OPTIMIZATION(aws): W-10 item 2``: its A2A runtime fails every turn without a
  sessions table, so it has one.

The buyer takes its credentials from its environment only (``auth_token_env``, an inline client secret): they are
visible to whoever may read the runtime's configuration (``bedrock-agentcore:GetAgentRuntime``).
"""

import json
from typing import Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import custom_resources as cr
from constructs import Construct

from fast_channels.core.selling.names import (
    Image,
    buyer_identity,
    buyer_sessions_table,
    governance_issuer,
    governance_signing_alias,
    governance_state_table,
    runtime,
)
from fast_channels.infra import nag
from fast_channels.infra.constructs.operator.runtime import model_invoke_statements

SCOPE: Final = "check"
"""The governance agent's one OAuth scope."""
SELLER_ID: Final = "fast-seller"
"""The seller's id in the buyer's registry."""
SELLER_TOKEN_ENV: Final = "FAST_SELLER_TOKEN"
BUYER_ID: Final = "buyer-a2a"
"""The buyer's own id in its registry (``agents_registry.BUYER_A2A_AGENT_ID``)."""
SIGNING_ALG: Final = "ecdsa-p256-sha256"
TTL_ATTRIBUTE: Final = "expires_at"
"""The governance table's one TTL attribute (its aggregate and idempotency items)."""


def invoke_url(scope: Construct, runtime_id: str) -> str:
    """The HTTPS invocation endpoint of a runtime's DEFAULT endpoint, its ARN URL-encoded from its parts."""
    stack = Stack.of(scope)
    encoded = "%3A".join(["arn", stack.partition, "bedrock-agentcore", stack.region, stack.account, "runtime%2F"])
    return (
        f"https://bedrock-agentcore.{stack.region}.{stack.url_suffix}/runtimes/{encoded}{runtime_id}/invocations"
        "?qualifier=DEFAULT"
    )


class Agents(Construct):
    """The governance agent with its key, table and token issuer, and the buyer with its sessions table.

    Args:
        prefix: deployment prefix (names).
        repositories: the images' repositories; ``tags``: each image's tag.
        seller_url: the seller's HTTPS base (``https://<CloudFront domain>``).
        seller_token: the reference buyer's bearer token at the seller.
        model: the buyer's Bedrock model (inference profile id).
        brand: the brand domain the buyer books for.
        log_retention: of the runtimes' log groups.

    Attributes:
        buyer, governance: the runtimes. governance_url: the governance agent's MCP endpoint.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        repositories: dict[Image, ecr.IRepository],
        tags: dict[Image, str],
        seller_url: str,
        seller_token: secretsmanager.ISecret,
        model: str,
        brand: str,
        log_retention: logs.RetentionDays,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.pool = cognito.UserPool(
            self,
            "Callers",
            self_sign_up_enabled=False,
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
        )
        check = cognito.ResourceServerScope(scope_name=SCOPE, scope_description="Check spend against a plan")
        server = self.pool.add_resource_server("Governance", identifier=f"{prefix}-governance", scopes=[check])
        self.client = self.pool.add_client(
            "Buyer",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[cognito.OAuthScope.resource_server(server, check)],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            access_token_validity=Duration.minutes(60),
            enable_token_revocation=True,
            prevent_user_existence_errors=True,
        )
        domain = self.pool.add_domain(
            "Tokens",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=f"{prefix}-selling-{stack.account}"),
        )
        self.key = kms.Key(
            self,
            "SigningKey",
            description="ES256 key of the governance agent's governance_context tokens",
            key_spec=kms.KeySpec.ECC_NIST_P256,
            key_usage=kms.KeyUsage.SIGN_VERIFY,
            alias=governance_signing_alias(prefix),
            pending_window=Duration.days(7),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.state = dynamodb.TableV2(
            self,
            "GovernanceState",
            table_name=governance_state_table(prefix),
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute=TTL_ATTRIBUTE,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.governance = agentcore.Runtime(
            self,
            "Governance",
            runtime_name=runtime(prefix, Image.GOVERNANCE),
            description="AWS reference governance agent (AdCP campaign governance, MCP)",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_ecr_repository(
                repositories[Image.GOVERNANCE], tags[Image.GOVERNANCE]
            ),
            protocol_configuration=agentcore.ProtocolType.MCP,
            authorizer_configuration=agentcore.RuntimeAuthorizerConfiguration.using_cognito(self.pool, [self.client]),
            request_header_configuration=agentcore.RequestHeaderConfiguration(allowlisted_headers=["Authorization"]),
            environment_variables={
                "STATE_TABLE_NAME": self.state.table_name,
                "GOVERNANCE_MODE": "enforce",
                "GOVERNANCE_SIGNING_KMS_KEY_ID": governance_signing_alias(prefix),
                "GOVERNANCE_SIGNING_ALG": SIGNING_ALG,
                "GOVERNANCE_SIGNING_KID": f"{prefix}-governance-1",
                "GOVERNANCE_AGENT_URL": governance_issuer(prefix),
                "AWS_REGION": stack.region,
            },
        )
        self.state.grant(
            self.governance, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"
        )
        self.governance.add_to_role_policy(
            iam.PolicyStatement(
                actions=["kms:Sign", "kms:GetPublicKey"],
                resources=[self.key.key_arn],
            )
        )
        self.governance_url = invoke_url(self, self.governance.agent_runtime_id)
        self.sessions = dynamodb.TableV2(
            self,
            "BuyerSessions",
            table_name=buyer_sessions_table(prefix),
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            global_secondary_indexes=[
                dynamodb.GlobalSecondaryIndexPropsV2(
                    index_name="gsi1",
                    partition_key=dynamodb.Attribute(name="gsi1pk", type=dynamodb.AttributeType.STRING),
                    sort_key=dynamodb.Attribute(name="gsi1sk", type=dynamodb.AttributeType.STRING),
                )
            ],
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        seller = {
            "auth_type": "static_bearer",
            "auth_token_env": SELLER_TOKEN_ENV,
            "agent_url": f"{seller_url}/a2a",
        }
        self.buyer = agentcore.Runtime(
            self,
            "Buyer",
            runtime_name=runtime(prefix, Image.BUYER),
            description="AWS reference buyer (AdCP buyer agent, A2A)",
            agent_runtime_artifact=agentcore.AgentRuntimeArtifact.from_ecr_repository(
                repositories[Image.BUYER], tags[Image.BUYER]
            ),
            protocol_configuration=agentcore.ProtocolType.A2_A,
            environment_variables={
                "SELLER_AGENTS_JSON": stack.to_json_string(
                    [
                        {
                            "id": SELLER_ID,
                            "name": f"FAST Channels seller ({prefix}, Prebid Sales Agent, A2A 1.0)",
                            "url": f"{seller_url}/a2a",
                            "transport": "a2a",
                            "a2a_version": "1.0",
                            **seller,
                        }
                    ]
                ),
                "DEFAULT_SELLER_AGENT_ID": SELLER_ID,
                SELLER_TOKEN_ENV: seller_token.secret_value.unsafe_unwrap(),
                "AGENTS_JSON": json.dumps(
                    [
                        {
                            "id": BUYER_ID,
                            "name": "AdCP Buyer Agent (A2A)",
                            "kind": "buyer",
                            "url": buyer_identity(prefix),
                            "auth_type": "cognito_bearer",
                            "origin": "internal",
                        }
                    ]
                ),
                "GOVERNANCE_AGENTS_JSON": stack.to_json_string(
                    [
                        {
                            "id": "fast-governance",
                            "name": f"FAST Channels governance ({prefix}, AWS reference governance agent)",
                            "url": self.governance_url,
                            "transport": "mcp",
                            "auth_type": "m2m_oauth",
                            "client_id": self.client.user_pool_client_id,
                            "client_secret": self.client.user_pool_client_secret.unsafe_unwrap(),
                            "token_url": f"{domain.base_url()}/oauth2/token",
                            "scope": f"{prefix}-governance/{SCOPE}",
                        }
                    ]
                ),
                "SESSIONS_TABLE_NAME": self.sessions.table_name,  # OPTIMIZATION(aws): W-10 item 2
                "BEDROCK_MODEL_ID": model,
                "BUYER_BRAND_DOMAIN": brand,
                "AWS_REGION": stack.region,
            },
        )
        self.sessions.grant(self.buyer, "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query")
        for statement in model_invoke_statements(
            [model], partition=stack.partition, region=stack.region, account=stack.account
        ):
            self.buyer.add_to_role_policy(statement)
        # AgentCore creates each endpoint's log group with the runtime, without retention; this sets it, and deletes
        # the group with the stack.
        for name, agent in (("BuyerLogs", self.buyer), ("GovernanceLogs", self.governance)):
            logs.LogRetention(
                self,
                name,
                log_group_name=f"/aws/bedrock-agentcore/runtimes/{agent.agent_runtime_id}-DEFAULT",
                retention=log_retention,
                removal_policy=RemovalPolicy.DESTROY,
            )
        self._acknowledge(stack, model)

    def _acknowledge(self, stack: Stack, model: str) -> None:
        sandbox = "A sandbox counterparty of one stage, destroyed with it"
        nag.acknowledge(
            self.pool,
            {
                "AwsSolutions-COG1": "No users: machine clients only (client credentials)",
                "AwsSolutions-COG2": "No users: machine clients only",
                "AwsSolutions-COG3": f"{sandbox}: no threat protection (Plus plan)",
                "AwsSolutions-COG8": f"{sandbox}: no users, so no Plus plan",
            },
        )
        # CDK's LogRetention provider, a stack-wide singleton without an accessor.
        retention = next(child for child in stack.node.children if child.node.id.startswith("LogRetention"))
        nag.acknowledge_references(
            retention,
            {
                nag.managed_policy("service-role/AWSLambdaBasicExecutionRole"): "CDK LogRetention provider: logs of "
                "its own function",
                nag.wildcard_resource(): "CDK LogRetention provider: one singleton sets retention policies for every "
                "log group of the stack",
                **{
                    nag.wildcard_resource(
                        f"arn:{stack.partition}:logs:{stack.region}:{stack.account}:log-group:"
                        f"/aws/bedrock-agentcore/runtimes/{agent.agent_runtime_id}-DEFAULT:*"
                    ): "CDK LogRetention provider: deletes the runtime's log group with the stack"
                    for agent in (self.buyer, self.governance)
                },
            },
        )
        # The client's secret is read by CDK's AwsCustomResource (DescribeUserPoolClient), a stack-wide singleton.
        provider = stack.node.try_find_child("AWS" + cr.AwsCustomResource.PROVIDER_FUNCTION_UUID.replace("-", ""))
        reason = "CDK AwsCustomResource provider (aws-cdk-lib shapes it), invoked synchronously by CloudFormation"
        if provider is not None:
            nag.acknowledge_references(
                provider,
                {
                    nag.LAMBDA_DLQ: reason,
                    "Serverless-LambdaDefaultMemorySize": reason,
                    nag.managed_policy("service-role/AWSLambdaBasicExecutionRole"): f"{reason}: its role",
                    nag.wildcard_resource(): f"{reason}: cognito-idp:DescribeUserPoolClient as CDK grants it",
                },
            )
        logs = f"arn:{stack.partition}:logs:{stack.region}:{stack.account}:log-group"
        identities = (
            f"arn:{stack.partition}:bedrock-agentcore:{stack.region}:{stack.account}:workload-identity-directory/"
            "default/workload-identity/*"
        )
        findings = {
            nag.wildcard_resource(): "Runtime L2 role: X-Ray segments, CloudWatch metrics (namespace-scoped) and "
            "ecr:GetAuthorizationToken only support '*'",
            nag.wildcard_resource(f"{logs}:/aws/bedrock-agentcore/runtimes/*"): "Runtime L2 role: the service names "
            "the runtime's log groups after its generated id",
            nag.wildcard_resource(f"{logs}:/aws/bedrock-agentcore/runtimes/*:log-stream:*"): "Runtime L2 role: log "
            "streams of the runtime's own log groups",
            nag.wildcard_resource(f"{logs}:*"): "Runtime L2 role: logs:DescribeLogGroups has no resource-level "
            "permissions",
            nag.wildcard_resource(identities): "Runtime L2 role: access tokens of the runtime's workload identity",
        }
        nag.acknowledge_references(self.governance.role, findings)
        prefix, _, model_id = model.partition(".")
        buyer = dict(findings)
        if prefix != "global":
            buyer[nag.wildcard_resource(f"arn:{stack.partition}:bedrock:*::foundation-model/{model_id}")] = (
                f"{prefix}. inference profile: routes to any region of its geography; allowed only through that "
                "profile (bedrock:InferenceProfileArn)"
            )
        buyer[nag.wildcard_resource(f"{self.sessions.table_arn}/index/*")] = "The buyer queries its sessions' index"
        nag.acknowledge_references(self.buyer.role, buyer)
