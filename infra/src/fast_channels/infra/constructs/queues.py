"""Dead-letter queues of asynchronous invocations and event deliveries."""

from typing import TYPE_CHECKING, Final

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_sqs as sqs

from fast_channels.infra import nag

if TYPE_CHECKING:
    from constructs import Construct

DEAD_LETTER_RETENTION: Final = Duration.days(14)
"""SQS's maximum: time to notice the alarm and redrive."""


def dead_letter_queue(scope: Construct, id: str, *, removal_policy: RemovalPolicy = RemovalPolicy.DESTROY) -> sqs.Queue:
    """A dead-letter queue: SSE-SQS, TLS only, messages kept :data:`DEAD_LETTER_RETENTION`.

    Alarm on it with :func:`fast_channels.infra.constructs.alerts.dead_letter_alarm`: what lands here left no other
    trace.
    """
    queue = sqs.Queue(
        scope,
        id,
        retention_period=DEAD_LETTER_RETENTION,
        encryption=sqs.QueueEncryption.SQS_MANAGED,
        enforce_ssl=True,
        removal_policy=removal_policy,
    )
    nag.acknowledge(
        queue,
        {
            nag.SQS_DLQ: "This queue is a dead-letter queue",
            "Serverless-SQSRedrivePolicy": "This queue is a dead-letter queue",
        },
    )
    return queue
