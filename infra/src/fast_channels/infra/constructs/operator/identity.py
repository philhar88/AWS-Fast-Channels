"""Cognito identity of the operator plane: operator users and groups, the console and autopilot app clients."""

from collections.abc import Sequence
from typing import Final, cast

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from constructs import Construct

from fast_channels.infra import nag
from fast_channels.infra.constructs.operator.cedar import Group

RESOURCE_SERVER: Final = "fast-channels"
INVOKE_SCOPE: Final = "agent.invoke"
AGENT_SCOPE: Final = f"{RESOURCE_SERVER}/{INVOKE_SCOPE}"
"""The OAuth scope the Gateway and the Runtime require of every access token."""
LOCAL_CONSOLE_URL: Final = "http://localhost:5173/"
"""Vite dev server of the console (console/README.md), allowed as a sign-in and sign-out redirect."""
_GROUP_DESCRIPTIONS: Final = {
    Group.VIEWERS: "Read channels, schedules, the library and ladders",
    Group.OPERATORS: "Viewers, plus plan and publish schedules, ingest, and start or stop channels",
    Group.ADMINS: "Operators, plus destructive tools on agent-owned resources",
}


def managed_login_url(domain_prefix: str, region: str) -> str:
    """Origin of the managed login pages of a Cognito domain prefix (no trailing slash)."""
    return f"https://{domain_prefix}.auth.{region}.amazoncognito.com"


class OperatorIdentity(Construct):
    """Cognito user pool (Essentials, managed login v2) for operators.

    - Users are created by administrators only and sign in with their email address. ``fast-viewers``,
      ``fast-operators`` and ``fast-admins`` drive the Gateway's Cedar policies (``cognito:groups`` claim).
    - ``console_client``: public SPA client, Authorization Code with PKCE, scopes ``openid email profile`` and
      :data:`AGENT_SCOPE`; refresh tokens rotate and can be revoked (the console revokes on sign-out).
    - ``autopilot_client``: confidential machine client (``client_credentials``, :data:`AGENT_SCOPE` only); the
      autopilot Lambda reads its secret with ``cognito-idp:DescribeUserPoolClient``.

    Args:
        domain_prefix: managed login domain prefix, unique in the region
            (``https://<prefix>.auth.<region>.amazoncognito.com``).
        callback_urls: console origins (``https://<host>/``) to redirect to after sign-in and sign-out;
            :data:`LOCAL_CONSOLE_URL` is always added.
        admin_email: email of the first ``fast-admins`` user, who receives a temporary password; ``None`` creates
            no user.
        removal_policy: what happens to the user pool (and its users) when the stack is deleted.

    Attributes:
        user_pool, console_client, autopilot_client, domain: the Cognito resources.
        domain_url: ``https://<prefix>.auth.<region>.amazoncognito.com`` (no trailing slash).
        token_endpoint: OAuth 2.0 token endpoint of the domain.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        domain_prefix: str,
        callback_urls: Sequence[str],
        admin_email: str | None = None,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.user_pool = cognito.UserPool(
            self,
            "Users",
            feature_plan=cognito.FeaturePlan.ESSENTIALS,  # managed login v2 needs Essentials (models-ui.md §6)
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False)
            ),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_digits=True,
                require_lowercase=True,
                require_uppercase=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            user_invitation=cognito.UserInvitationConfig(
                email_subject="Your FAST Channels operator console account",
                email_body=(
                    "You can now sign in to the FAST Channels operator console with the username {username} and the "
                    f"temporary password {{####}}. Console: {callback_urls[0] if callback_urls else ''}"
                ),
            ),
            deletion_protection=removal_policy is RemovalPolicy.RETAIN,
            removal_policy=removal_policy,
        )
        invoke = cognito.ResourceServerScope(scope_name=INVOKE_SCOPE, scope_description="Use the scheduling agent")
        server = self.user_pool.add_resource_server("AgentApi", identifier=RESOURCE_SERVER, scopes=[invoke])
        agent_scope = cognito.OAuthScope.resource_server(server, invoke)
        redirects = [*callback_urls, LOCAL_CONSOLE_URL]
        self.console_client = self.user_pool.add_client(
            "Console",
            generate_secret=False,  # public SPA client: Authorization Code + PKCE
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE, agent_scope],
                callback_urls=redirects,
                logout_urls=redirects,
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            access_token_validity=Duration.minutes(60),
            id_token_validity=Duration.minutes(60),
            refresh_token_validity=Duration.hours(12),
            refresh_token_rotation_grace_period=Duration.seconds(30),
            enable_token_revocation=True,
            prevent_user_existence_errors=True,
        )
        self.autopilot_client = self.user_pool.add_client(
            "Autopilot",
            generate_secret=True,
            o_auth=cognito.OAuthSettings(flows=cognito.OAuthFlows(client_credentials=True), scopes=[agent_scope]),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            access_token_validity=Duration.minutes(60),
            enable_token_revocation=True,
            prevent_user_existence_errors=True,
        )
        self.domain = self._managed_login(domain_prefix)
        self.domain_url = managed_login_url(domain_prefix, Stack.of(self).region)
        self.token_endpoint = f"{self.domain_url}/oauth2/token"
        groups = {group: self._group(group, precedence) for precedence, group in enumerate(reversed(Group))}
        if admin_email:
            self._admin(admin_email, groups[Group.ADMINS])
        nag.acknowledge(
            self.user_pool,
            {
                "AwsSolutions-COG2": "MFA (TOTP) is optional so that the first sign-in works without an authenticator; "
                "enforce it per organisation policy",
                "AwsSolutions-COG3": "Threat protection (advanced security) needs the Plus feature plan; the pool is "
                "admin-invite only",
                "AwsSolutions-COG8": "Essentials includes managed login v2; Plus adds threat protection at higher "
                "per-user cost, an organisation choice",
            },
        )

    @property
    def discovery_url(self) -> str:
        """OpenID Connect discovery document of the user pool (JWT authorizers of the Gateway and Runtime)."""
        stack = Stack.of(self)
        return (
            f"https://cognito-idp.{stack.region}.{stack.url_suffix}/{self.user_pool.user_pool_id}"
            "/.well-known/openid-configuration"
        )

    def _managed_login(self, domain_prefix: str) -> cognito.UserPoolDomain:
        """Managed login v2 with Cognito's default branding.

        Creating the domain with ``ManagedLoginVersion: 2`` together with the branding races inside Cognito
        ("Internal error from downstream service"); the FAST template's ordering avoids it: domain without a version,
        branding after the domain, then version 2 set on the domain (models-ui.md §5.1, §6).
        """
        domain = self.user_pool.add_domain(
            "Login", cognito_domain=cognito.CognitoDomainOptions(domain_prefix=domain_prefix)
        )
        branding = cognito.CfnManagedLoginBranding(
            self,
            "Branding",
            user_pool_id=self.user_pool.user_pool_id,
            client_id=self.console_client.user_pool_client_id,
            use_cognito_provided_values=True,
        )
        branding.node.add_dependency(domain)
        cast("cognito.CfnUserPoolDomain", domain.node.default_child).managed_login_version = 2
        return domain

    def _group(self, group: Group, precedence: int) -> cognito.CfnUserPoolGroup:
        return cognito.CfnUserPoolGroup(
            self,
            group.name.title(),
            user_pool_id=self.user_pool.user_pool_id,
            group_name=group,
            description=_GROUP_DESCRIPTIONS[group],
            precedence=precedence,
        )

    def _admin(self, email: str, admins: cognito.CfnUserPoolGroup) -> None:
        user = cognito.CfnUserPoolUser(
            self,
            "Admin",
            user_pool_id=self.user_pool.user_pool_id,
            username=email,
            user_attributes=[
                cognito.CfnUserPoolUser.AttributeTypeProperty(name="email", value=email),
                cognito.CfnUserPoolUser.AttributeTypeProperty(name="email_verified", value="true"),
            ],
            desired_delivery_mediums=["EMAIL"],
        )
        membership = cognito.CfnUserPoolUserToGroupAttachment(
            self, "AdminMembership", user_pool_id=self.user_pool.user_pool_id, group_name=admins.ref, username=user.ref
        )
        membership.add_resource_dependency(admins)
