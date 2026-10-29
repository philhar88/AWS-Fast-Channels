"""The stage's AWS AppConfig application, which the ops module's feature flags (M1-05) and the Release Controller's
release pointer (M3-04) add configuration profiles to."""

from aws_cdk import aws_appconfig as appconfig
from constructs import Construct


class OpsAppConfig(Construct):
    """AppConfig application ``<prefix>-fast`` with one environment, ``<prefix>``.

    The environment skips AppConfig's deletion protection check (``BYPASS``): a stage is destroyed right after its
    end-to-end run, when the protection (if the account turns it on) would still count the profiles as in use.

    Args:
        prefix: deployment prefix.

    Attributes:
        application: the application.
        environment: its environment.
    """

    def __init__(self, scope: Construct, id: str, *, prefix: str) -> None:
        super().__init__(scope, id)
        self.application = appconfig.Application(
            self,
            "Application",
            application_name=f"{prefix}-fast",
            description=f"FAST Channels ({prefix}): feature flags and the release pointer",
        )
        self.environment = appconfig.Environment(
            self,
            "Environment",
            application=self.application,
            environment_name=prefix,
            description=f"The {prefix} stage",
            deletion_protection_check=appconfig.DeletionProtectionCheck.BYPASS,
        )
