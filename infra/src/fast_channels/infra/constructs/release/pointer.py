"""The release pointer (DESIGN §8.4): the AppConfig profile ``release`` in the stage's application, the deployment
strategy of the stage's rollout tier, and the composite alarm the environment monitors.

The profile holds ``{"<channel>": "<stable sibling>", …}``. The Release Controller creates its versions and deploys
them with the strategy: LINEAR or EXPONENTIAL with every step longer than 2 minutes, never all at once, because a
0-minute deployment ignores the monitors (P0-08 §3.5). From M4 the mint reads the pointer with ``Entity-Id`` and so
moves viewers progressively; in Phase 1 the deployment is the record of the promotion.

The environment (Ops stack) monitors ``<prefix>-release-candidate``: any alarm of the pair siblings (the candidate is
one of them; the stable one going to ALARM during a rollout stops it too). Every child alarm treats missing data as
not breaching: a candidate has no traffic before the pointer moves to it, and an alarm in ``INSUFFICIENT_DATA`` rolled
a deployment back after 5 s (P0-08).
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

from aws_cdk import aws_appconfig as appconfig
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from constructs import Construct

from fast_channels.core.monetization import names

if TYPE_CHECKING:
    from fast_channels.core.monetization.bundle import Rollout


class ReleasePointer(Construct):
    """The ``release`` profile, the strategy and the candidate composite alarm.

    Args:
        prefix: deployment prefix.
        application_id: the AppConfig application ``<prefix>-fast`` (from the Ops stack's SSM parameter).
        rollout: the bundle's rollout strategy (``modules.release.strategy``).
        alarms: the pair siblings' alarms (hook errors, markers without an ad request).
        topic: the stage's alert topic.

    Attributes:
        profile: the configuration profile. strategy: the deployment strategy. at_once: the strategy of a pointer
        that moves no viewer or goes back. candidate: the composite alarm.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        application_id: str,
        rollout: Rollout,
        alarms: Sequence[cw.IAlarm],
        topic: sns.ITopic,
    ) -> None:
        super().__init__(scope, id)
        self.profile = appconfig.CfnConfigurationProfile(
            self,
            "Profile",
            application_id=application_id,
            name=names.POINTER_PROFILE,
            location_uri="hosted",
            type="AWS.Freeform",
            description="Each monetized channel's stable sibling playback configuration (the Release Controller's)",
            deletion_protection_check="BYPASS",
        )
        self.strategy = appconfig.CfnDeploymentStrategy(
            self,
            "Strategy",
            name=f"{prefix}-release-{rollout.name}",
            description=rollout.description,
            growth_type=rollout.growth_type,
            growth_factor=rollout.growth_factor,
            deployment_duration_in_minutes=rollout.deployment_duration_in_minutes,
            final_bake_time_in_minutes=rollout.final_bake_time_in_minutes,
            replicate_to="NONE",
        )
        self.at_once = appconfig.CfnDeploymentStrategy(
            self,
            "AtOnce",
            name=f"{prefix}-release-at-once",
            description="A pointer that moves no viewer (a first release) or goes back (a rollback): at once, "
            "with no bake (AppConfig.AllAtOnce bakes 10 minutes, during which no other deployment can start)",
            growth_type="LINEAR",
            growth_factor=100,
            deployment_duration_in_minutes=0,
            final_bake_time_in_minutes=0,
            replicate_to="NONE",
        )
        self.candidate = cw.CompositeAlarm(
            self,
            "Candidate",
            composite_alarm_name=names.candidate_alarm(prefix),
            alarm_description="A pair sibling of a monetized channel is in ALARM: a release pointer deployment in "
            "progress rolls back (AppConfig monitor), and runbook R1 rolls the sibling back",
            alarm_rule=cw.AlarmRule.any_of(*(cw.AlarmRule.from_alarm(a, cw.AlarmState.ALARM) for a in alarms)),
        )
        self.candidate.add_alarm_action(cw_actions.SnsAction(topic))
