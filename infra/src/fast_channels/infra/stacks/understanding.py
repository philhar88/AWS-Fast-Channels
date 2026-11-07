"""``FastChannels-Understanding``: content understanding, deployed when ``modules.understanding.enabled`` (M12)."""

from typing import TYPE_CHECKING, Any

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_sns as sns

from fast_channels.infra.constructs.alerts import alert_topic_name
from fast_channels.infra.constructs.understanding.workflow import UnderstandingWorkflow

if TYPE_CHECKING:
    from constructs import Construct

    from fast_channels.infra.config import FastChannelsConfig
    from fast_channels.infra.exports import DataExports, MediaExports, ModelsExports


class UnderstandingStack(Stack):
    """The understanding workflow: on a title's first ``Asset Ready``, a proxy, the configured Transcribe and
    Rekognition Video analyses and the model task's description, written as the title's ``UNDERSTANDING`` item in
    the catalog table.

    Args:
        config: deployment configuration (``modules.understanding``, ``observability``, ``removal``).
        data: Data stack references (the tables, the media and logs buckets).
        media: Media stack exports (the bus). The alert topic is imported by its name.
        models: Models stack exports (the task's profile).

    Raises:
        ValueError: the model registry has no profile for the module's task.

    Attributes:
        workflow: the workflow.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        config: FastChannelsConfig,
        data: DataExports,
        media: MediaExports,
        models: ModelsExports,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, id, description=f"FAST Channels ({config.prefix}): content understanding", **kwargs)
        module = config.modules.understanding
        if (profile := models.profiles.get(module.task)) is None:
            raise ValueError(f"the model registry has no task {module.task!r} (modules.models.tasks)")
        self.workflow = UnderstandingWorkflow(
            self,
            "Workflow",
            prefix=config.prefix,
            data=data,
            event_bus=media.event_bus,
            alert_topic=sns.Topic.from_topic_arn(
                self, "Alerts", self.format_arn(service="sns", resource=alert_topic_name(config.prefix))
            ),
            profile=profile,
            task=module.task,
            analyses=module.analyses,
            log_retention=config.observability.log_retention,
            removal_policy=config.removal_policy,
        )
        outputs = {
            "UnderstandingStateMachineArn": (
                self.workflow.state_machine.state_machine_arn,
                'The workflow; start it with {"detail": <Asset Ready detail>, "force": true} to analyse a title again',
            ),
            "UnderstandingBucketName": (
                self.workflow.bucket.bucket_name,
                "Analysis proxies (proxies/, 1 day) and raw results (results/, 30 days)",
            ),
        }
        for name, (value, description) in outputs.items():
            CfnOutput(self, name, value=value, description=description)
