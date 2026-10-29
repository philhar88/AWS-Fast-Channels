"""Alerting: the SNS topic that alarms and failure events notify, and the alarm recipes every plane uses (SPEC §9).

Alarms notify their topic of both state changes (ALARM and OK) and treat missing data as not breaching. An alarm
must live in its metric's region, and alarms publish to topics of their own region: CloudFront metrics exist in
us-east-1 only (:data:`CLOUDFRONT_REGION`).
"""

from typing import TYPE_CHECKING, Final

from aws_cdk import Duration
from aws_cdk import aws_cloudfront as cf
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subscriptions
from aws_cdk import aws_sqs as sqs

from fast_channels.infra import nag

if TYPE_CHECKING:
    from constructs import Construct

CLOUDFRONT_REGION: Final = "us-east-1"
"""Region of CloudFront's metrics and log delivery sources."""
PERIOD: Final = Duration.minutes(5)
"""Period of every alarm and dashboard metric unless stated otherwise."""
_SNS_SSE: Final = "AwsSolutions-SNS2"
_SNS_REDRIVE: Final = "Serverless-SNSRedrivePolicy"


def alert_topic_name(prefix: str) -> str:
    """Name of the media plane's alert topic. The stacks of optional modules import the topic by this name, so
    enabling a module adds no output to the Media stack."""
    return f"{prefix}-alerts"


def alert_topic(scope: Construct, id: str, *, name: str, email: str | None) -> sns.Topic:
    """SNS topic of alarms and failure events, optionally with an email subscription (confirmed by the
    recipient). EventBridge rules publish to it through a role of their own, never as a service principal (see
    :class:`~fast_channels.infra.constructs.observability.MediaObservability`)."""
    topic = sns.Topic(scope, id, topic_name=name, display_name="FAST Channels alerts", enforce_ssl=True)
    nag.acknowledge(
        topic,
        {
            _SNS_SSE: "Alarm and event notifications carry no secrets; CloudWatch alarms cannot publish to topics "
            "encrypted with the AWS managed key, and a customer managed key would cost more than it protects"
        },
    )
    if email:
        topic.add_subscription(subscriptions.EmailSubscription(email))
        nag.acknowledge(
            topic,
            {_SNS_REDRIVE: "Email delivery: alarm states and the event archive remain the record of a lost email"},
        )
    return topic


def notify(alarm: cw.Alarm, topic: sns.ITopic) -> None:
    """Notify ``topic`` when ``alarm`` goes to ALARM and when it returns to OK."""
    action = cw_actions.SnsAction(topic)
    alarm.add_alarm_action(action)
    alarm.add_ok_action(action)


def alarm(
    scope: Construct,
    id: str,
    *,
    name: str,
    metric: cw.IMetric,
    threshold: float,
    description: str,
    topic: sns.ITopic,
    comparison: cw.ComparisonOperator = cw.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
    evaluation_periods: int = 1,
) -> cw.Alarm:
    """Alarm ``name`` on ``metric`` (missing data is not breaching), notifying ``topic`` (:func:`notify`)."""
    created = cw.Alarm(
        scope,
        id,
        alarm_name=name,
        alarm_description=description,
        metric=metric,
        threshold=threshold,
        evaluation_periods=evaluation_periods,
        comparison_operator=comparison,
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )
    notify(created, topic)
    return created


def dead_letter_alarm(
    scope: Construct, id: str, *, queue: sqs.IQueue, name: str, description: str, topic: sns.ITopic
) -> cw.Alarm:
    """Alarm while ``queue`` holds a message: whatever failed into it left no other trace (no execution, no event).

    It stays in ALARM until the messages are redriven or purged.
    """
    return alarm(
        scope,
        id,
        name=name,
        metric=queue.metric_approximate_number_of_messages_visible(period=PERIOD, statistic=cw.Stats.MAXIMUM),
        threshold=1,
        description=description,
        topic=topic,
    )


def cdn_metric(distribution: cf.IDistribution, name: str, *, statistic: str) -> cw.Metric:
    """A CloudFront distribution metric (published in us-east-1 for the ``Global`` region)."""
    return cw.Metric(
        namespace="AWS/CloudFront",
        metric_name=name,
        dimensions_map={"DistributionId": distribution.distribution_id, "Region": "Global"},
        statistic=statistic,
        period=PERIOD,
        region=CLOUDFRONT_REGION,
    )


def cdn_error_alarm(
    scope: Construct, id: str, *, distribution: cf.IDistribution, topic: sns.ITopic, name: str
) -> cw.Alarm:
    """Alarm on the distribution's 5xx error rate. CloudFront publishes metrics in us-east-1 only, and an alarm
    must be in its metric's region: create it in a us-east-1 stack (Media there, otherwise Edge)."""
    created = cw.Alarm(
        scope,
        id,
        alarm_name=name,
        alarm_description="CloudFront 5xx responses above 5 % (MediaTailor or origin failures reach viewers)",
        metric=cdn_metric(distribution, "5xxErrorRate", statistic=cw.Stats.AVERAGE),
        threshold=5,
        evaluation_periods=3,
        datapoints_to_alarm=2,
        comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
    )
    notify(created, topic)
    return created
