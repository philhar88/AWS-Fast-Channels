"""The viewer pool: Cognito users of the viewer app, and the access tokens ``POST /playback`` accepts."""

from typing import Final

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_logs as logs
from constructs import Construct

from fast_channels.core.identity.names import (
    PERSONA_ATTRIBUTE,
    PLAYBACK_SCOPE_NAME,
    RESOURCE_SERVER,
    user_pool_name,
)
from fast_channels.infra import nag
from fast_channels.infra.constructs.functions import FastFunction

ACCESS_TOKEN_MINUTES: Final = 60


class ViewerPool(Construct):
    """Cognito user pool (Essentials) of the stage's viewers.

    - Administrators create the users (``scripts/seed_personas.py`` for the demo personas); there is no self sign-up
      and no email is sent. Users sign in with their email address and a password, by SRP or ``USER_PASSWORD_AUTH``.
    - ``custom:persona`` holds a demo persona id (``contracts/personas.json``).
    - The resource server ``fast`` defines the scope ``fast/playback``.
    - The pre-token-generation trigger (event version 2, which needs Essentials) adds that scope and the claim
      ``fast:persona`` to every access token (``fast_channels.functions.identity.pre_token``). Password and SRP
      sign-in grant no custom scope otherwise, and ID tokens never carry one.
    - ``client``: the viewer app's public client, no secret; the e2e tests sign in with it too. It may read
      ``custom:persona`` (the app shows the persona from the ID token).

    Args:
        prefix: deployment prefix (``<prefix>-viewers``).
        log_retention: of the trigger's logs.
        removal_policy: what happens to the pool (and its users) when the stack is deleted.

    Attributes:
        user_pool, resource_server, client: the Cognito resources.
        scope: the ``fast/playback`` OAuth scope.
        trigger: the pre-token-generation function.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        log_retention: logs.RetentionDays,
        removal_policy: RemovalPolicy = RemovalPolicy.DESTROY,
    ) -> None:
        super().__init__(scope, id)
        self.user_pool = cognito.UserPool(
            self,
            "Users",
            user_pool_name=user_pool_name(prefix),
            feature_plan=cognito.FeaturePlan.ESSENTIALS,  # access-token customisation (pre token generation v2)
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False)
            ),
            custom_attributes={PERSONA_ATTRIBUTE: cognito.StringAttribute(min_len=1, max_len=32, mutable=True)},
            mfa=cognito.Mfa.OFF,
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_digits=True,
                require_lowercase=True,
                require_uppercase=True,
                require_symbols=True,
            ),
            account_recovery=cognito.AccountRecovery.NONE,
            deletion_protection=removal_policy is RemovalPolicy.RETAIN,
            removal_policy=removal_policy,
        )
        playback = cognito.ResourceServerScope(
            scope_name=PLAYBACK_SCOPE_NAME, scope_description="Mint a playback ticket for the signed-in viewer"
        )
        self.resource_server = self.user_pool.add_resource_server(
            "Playback", identifier=RESOURCE_SERVER, scopes=[playback]
        )
        self.scope = cognito.OAuthScope.resource_server(self.resource_server, playback)
        self.client = self.user_pool.add_client(
            "Viewers",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True, user_password=True),
            read_attributes=cognito.ClientAttributes()
            .with_standard_attributes(email=True, email_verified=True)
            .with_custom_attributes(PERSONA_ATTRIBUTE),
            write_attributes=cognito.ClientAttributes(),
            access_token_validity=Duration.minutes(ACCESS_TOKEN_MINUTES),
            id_token_validity=Duration.minutes(ACCESS_TOKEN_MINUTES),
            refresh_token_validity=Duration.hours(12),
            enable_token_revocation=True,
            prevent_user_existence_errors=True,
        )
        self.trigger = FastFunction(
            self,
            "PreToken",
            handler="fast_channels.functions.identity.pre_token.handler",
            description="Viewer pool: adds fast/playback and the persona to access tokens",
            timeout=Duration.seconds(5),
            memory_size=256,
            log_retention=log_retention,
            compiled=True,
        )
        nag.acknowledge(self.trigger, {nag.LAMBDA_DLQ: "Invoked synchronously by Cognito at sign-in"})
        self.user_pool.add_trigger(
            cognito.UserPoolOperation.PRE_TOKEN_GENERATION_CONFIG,
            self.trigger.function,
            cognito.LambdaVersion.V2_0,
        )
        nag.acknowledge(
            self.user_pool,
            {
                "AwsSolutions-COG2": "Demo viewers sign in with an administrator-set password; a production pool adds "
                "MFA or federation per organisation policy",
                "AwsSolutions-COG3": "Threat protection (advanced security) needs the Plus feature plan; the pool is "
                "administrator-created only",
                "AwsSolutions-COG8": "Essentials includes access-token customisation; Plus adds threat protection at "
                "higher per-user cost, an organisation choice",
            },
        )
