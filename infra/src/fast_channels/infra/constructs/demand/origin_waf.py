"""``OriginGate``: only the stage's MediaTailor requests reach Prebid Server (DESIGN §6.10).

Two origin secrets, both valid at once so a rotation never blocks a POST, and a CLOUDFRONT-scope WAF rule group that
the guidance's web ACL references first (overlay O2, parameter ``ExtraWafRuleGroupArn``). Rotating a secret changes
this rule group only, never the guidance's web ACL, which the guidance deletes with its creation-time lock token.
"""

from typing import Final

from aws_cdk import RemovalPolicy, SecretValue
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_wafv2 as wafv2
from constructs import Construct

from fast_channels.core.demand import ORIGIN_HEADER, ORIGIN_SECRET_SLOTS, origin_rule_group, origin_secret
from fast_channels.infra import nag

AUCTION_PREFIX: Final = "/openrtb2/"
OPEN_PATHS: Final = ("/openrtb2/auction", "/status")
"""The only paths that reach Prebid Server: MediaTailor's auction and the health check."""
CAPACITY: Final = 50
"""Web ACL capacity units reserved for the group (immutable; the rules use about 20). The guidance's own rules use
about 1,300, so the web ACL stays under the 1,500 units included in its price."""


def _match(
    field: wafv2.CfnRuleGroup.FieldToMatchProperty, value: str, *, position: str
) -> wafv2.CfnRuleGroup.StatementProperty:
    return wafv2.CfnRuleGroup.StatementProperty(
        byte_match_statement=wafv2.CfnRuleGroup.ByteMatchStatementProperty(
            field_to_match=field,
            positional_constraint=position,
            search_string=value,
            text_transformations=[wafv2.CfnRuleGroup.TextTransformationProperty(priority=0, type="NONE")],
        )
    )


def _path(value: str, position: str) -> wafv2.CfnRuleGroup.StatementProperty:
    return _match(wafv2.CfnRuleGroup.FieldToMatchProperty(uri_path={}), value, position=position)


def _visibility(name: str) -> wafv2.CfnRuleGroup.VisibilityConfigProperty:
    return wafv2.CfnRuleGroup.VisibilityConfigProperty(
        cloud_watch_metrics_enabled=True, metric_name=name, sampled_requests_enabled=True
    )


def _block(
    name: str, priority: int, statement: wafv2.CfnRuleGroup.StatementProperty
) -> wafv2.CfnRuleGroup.RuleProperty:
    return wafv2.CfnRuleGroup.RuleProperty(
        name=name,
        priority=priority,
        action=wafv2.CfnRuleGroup.RuleActionProperty(block={}),
        statement=statement,
        visibility_config=_visibility(name),
    )


class OriginGate(Construct):
    """The origin secrets and the rule group that requires one of them.

    Rules, in order:

    - ``fast-origin-required``: block ``/openrtb2/*`` unless ``x-fast-origin`` equals origin secret A or B;
    - ``fast-paths``: block every path but ``/openrtb2/auction`` and ``/status``, so the guidance's other public
      Prebid Server endpoints (cache writes, user sync, events) are closed;
    - ``fast-rate``: block a client IP above ``rate_limit_per_ip`` requests to ``/openrtb2/*`` per 60 s, an abuse
      cap only: MediaTailor spreads its POSTs over many addresses.

    The rule group reads the secrets through CloudFormation dynamic references: no secret value is in a template.

    Args:
        prefix: deployment prefix (names).
        rate_limit_per_ip: ``modules.demand.rate_limit_per_ip``.
        removal_policy: of the secrets.

    Attributes:
        secrets: origin secret A and B.
        rule_group: the WAF rule group (CLOUDFRONT scope; its stack must be in us-east-1).
    """

    def __init__(
        self, scope: Construct, id: str, *, prefix: str, rate_limit_per_ip: int, removal_policy: RemovalPolicy
    ) -> None:
        super().__init__(scope, id)
        self.secrets = [
            secretsmanager.Secret(
                self,
                f"Origin{slot.upper()}",
                secret_name=origin_secret(prefix, slot),
                description=f"Origin secret {slot.upper()}, sent by MediaTailor to the demand origin ({ORIGIN_HEADER})",
                generate_secret_string=secretsmanager.SecretStringGenerator(
                    password_length=48, exclude_punctuation=True, include_space=False
                ),
                removal_policy=removal_policy,
            )
            for slot in ORIGIN_SECRET_SLOTS
        ]
        for secret in self.secrets:
            nag.acknowledge(
                secret,
                {
                    "AwsSolutions-SMG4": "Rotated by the release module's two-value rotation (both secrets stay valid "
                    "while one changes), not by a Secrets Manager rotation Lambda",
                },
            )
        header = wafv2.CfnRuleGroup.FieldToMatchProperty(single_header={"Name": ORIGIN_HEADER})
        secret_matches = [
            _match(header, SecretValue.secrets_manager(secret.secret_arn).unsafe_unwrap(), position="EXACTLY")
            for secret in self.secrets
        ]
        name = origin_rule_group(prefix)
        self.rule_group = wafv2.CfnRuleGroup(
            self,
            "RuleGroup",
            name=name,
            scope="CLOUDFRONT",
            capacity=CAPACITY,
            description="FAST Channels: only MediaTailor requests of the stage reach Prebid Server",
            visibility_config=_visibility(name),
            rules=[
                _block(
                    "fast-origin-required",
                    0,
                    wafv2.CfnRuleGroup.StatementProperty(
                        and_statement=wafv2.CfnRuleGroup.AndStatementProperty(
                            statements=[
                                _path(AUCTION_PREFIX, "STARTS_WITH"),
                                wafv2.CfnRuleGroup.StatementProperty(
                                    not_statement=wafv2.CfnRuleGroup.NotStatementProperty(
                                        statement=wafv2.CfnRuleGroup.StatementProperty(
                                            or_statement=wafv2.CfnRuleGroup.OrStatementProperty(
                                                statements=secret_matches
                                            )
                                        )
                                    )
                                ),
                            ]
                        )
                    ),
                ),
                _block(
                    "fast-paths",
                    1,
                    wafv2.CfnRuleGroup.StatementProperty(
                        not_statement=wafv2.CfnRuleGroup.NotStatementProperty(
                            statement=wafv2.CfnRuleGroup.StatementProperty(
                                or_statement=wafv2.CfnRuleGroup.OrStatementProperty(
                                    statements=[_path(path, "EXACTLY") for path in OPEN_PATHS]
                                )
                            )
                        )
                    ),
                ),
                _block(
                    "fast-rate",
                    2,
                    wafv2.CfnRuleGroup.StatementProperty(
                        rate_based_statement=wafv2.CfnRuleGroup.RateBasedStatementProperty(
                            aggregate_key_type="IP",
                            limit=rate_limit_per_ip,
                            evaluation_window_sec=60,
                            scope_down_statement=_path(AUCTION_PREFIX, "STARTS_WITH"),
                        )
                    ),
                ),
            ],
        )
