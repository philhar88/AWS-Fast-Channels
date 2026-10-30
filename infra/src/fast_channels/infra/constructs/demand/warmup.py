"""``WarmUp``: a synthetic auction replay against every new Prebid Server task (P0-21).

A fresh PBS-Java task collapses under a break herd until its JIT has warmed. Whenever a task of the guidance's
Prebid Server service reaches ``RUNNING`` (the census gate's scale-out, a daypart action or target tracking), an
EventBridge rule starts a CodeBuild build with the task's ARN. The build waits until the task is a healthy target of
the guidance's load balancer: load on a task that is not listening yet, or is still being health-checked, fails its
checks and ECS replaces it. It then replays MediaTailor's POST through the guidance's CloudFront at
``WARMUP_RATE_PER_TASK`` auctions per second per healthy target (the load balancer spreads the replay, so the new task
gets about that rate) for ``WARMUP_SECONDS``, with origin secret A. Every request is a probe (``X-Fast-Probe: 1``)
without a ticket, so the Pod Decision Service answers own-book and house only: no partner sees the replay. The load
generator is vegeta, pinned and checksum-verified.
"""

import json
from typing import Any, Final

from aws_cdk import ArnFormat, Duration, RemovalPolicy, Stack
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from fast_channels.core.demand import (
    ORIGIN_HEADER,
    VEGETA,
    WARMUP_HEALTHY_WAIT_SECONDS,
    WARMUP_RATE_PER_TASK,
    WARMUP_SECONDS,
    origin_secret,
    warmup_project,
)
from fast_channels.core.pbs_config import POD_MAX_SECONDS, auction_body
from fast_channels.infra import nag
from fast_channels.infra.grants import allow

EDGE_REGION: Final = "us-east-1"
"""Where the origin secrets live (DemandEdge)."""


def warmup_buildspec(*, prefix: str, cloudfront_domain: str, cluster_arn: str, target_group_arn: str) -> dict[str, Any]:
    """The warm-up build: fetch vegeta (checksum-verified), wait until the task (``TASK_ARN``, when the rule starts
    it) is a healthy target, read origin secret A, replay probe auctions (``RATE`` and ``SECONDS_TO_RUN`` override
    the rate and the duration, as ``prebid_guidance.py herd`` does)."""
    polls = WARMUP_HEALTHY_WAIT_SECONDS // 10
    longest = POD_MAX_SECONDS
    query = f"evid=1&upid=warmup&dur={longest * 1001}"
    body = json.dumps(auction_body(longest), separators=(",", ":"))
    return {
        "version": "0.2",
        "env": {"shell": "bash"},
        "phases": {
            "install": {
                "commands": [
                    f'curl -fsSL -o vegeta.tar.gz "{VEGETA["url"]}"',
                    f'echo "{VEGETA["sha256"]}  vegeta.tar.gz" | sha256sum -c -',
                    "tar -xzf vegeta.tar.gz vegeta && ./vegeta -version",
                ]
            },
            "pre_build": {
                "commands": [
                    'if [ -n "${TASK_ARN:-}" ]; then '
                    f'IP=$(aws ecs describe-tasks --cluster {cluster_arn} --tasks "$TASK_ARN" '
                    """--query "tasks[0].attachments[0].details[?name=='privateIPv4Address'].value | [0]" """
                    "--output text); "
                    f"for i in $(seq 1 {polls}); do "
                    f"STATE=$(aws elbv2 describe-target-health --target-group-arn {target_group_arn} "
                    # By address alone: a target is registered on the container port, not the group's default
                    """--query "TargetHealthDescriptions[?Target.Id=='$IP'].TargetHealth.State | [0]" """
                    "--output text); "
                    '[ "$STATE" = healthy ] && break; '
                    '[ "$STATE" = draining ] && { echo "task $TASK_ARN is draining: no warm-up"; SKIP=1; break; }; '
                    "sleep 10; done; "
                    '[ "$STATE" = healthy ] || [ "${SKIP:-0}" = 1 ] || { echo "task $TASK_ARN ($IP) is $STATE after '
                    f'{WARMUP_HEALTHY_WAIT_SECONDS} s"; exit 1; }}; fi',
                    f"HEALTHY=$(aws elbv2 describe-target-health --target-group-arn {target_group_arn} "
                    """--query "length(TargetHealthDescriptions[?TargetHealth.State=='healthy'])" --output text)""",
                    f"RATE=${{RATE:-$(( {WARMUP_RATE_PER_TASK} * (HEALTHY > 0 ? HEALTHY : 1) ))}}; "
                    'echo "replaying $RATE/s over $HEALTHY healthy targets"',
                ]
            },
            "build": {
                "commands": [
                    f"SECRET=$(aws secretsmanager get-secret-value --region {EDGE_REGION} "
                    f"--secret-id {origin_secret(prefix, 'a')} --query SecretString --output text)",
                    f"printf '%s' '{body}' > body.json",
                    '{ echo "POST https://' + cloudfront_domain + "/openrtb2/auction?" + query + '"; '
                    f'echo "{ORIGIN_HEADER}: $SECRET"; echo "Content-Type: application/json"; '
                    'echo "X-Fast-Probe: 1"; echo "X-Fast-Sid: warmup-$CODEBUILD_BUILD_NUMBER"; '
                    f'echo "X-Fast-Pc: {prefix}-warmup"; echo "@body.json"; }} > targets.txt',
                    'if [ "${SKIP:-0}" != 1 ]; then ./vegeta attack -targets=targets.txt -rate=${RATE}/s '
                    f"-duration=${{SECONDS_TO_RUN:-{WARMUP_SECONDS}}}s -timeout=5s > results.bin && "
                    "./vegeta report results.bin && "
                    'echo "VEGETA_JSON $(./vegeta report -type=json results.bin)"; fi',
                ]
            },
        },
    }


class WarmUp(Construct):
    """The warm-up project and the rule that starts it for every new Prebid Server task.

    Args:
        prefix: the stage's prefix.
        cluster_arn: the guidance's ECS cluster.
        service_arn: the guidance's Prebid Server service.
        cloudfront_domain: the guidance's CloudFront domain (the replay's target).
        target_group_arn: the guidance's Prebid Server target group (whose health the build waits for).
        log_retention: the builds' log retention.
        dead_letters: where undeliverable new-task events go.

    Attributes:
        project: the warm-up project.
        rule: the ``ECS Task State Change`` rule.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        cluster_arn: str,
        service_arn: str,
        cloudfront_domain: str,
        target_group_arn: str,
        log_retention: logs.RetentionDays,
        dead_letters: sqs.IQueue,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/codebuild/{warmup_project(prefix)}",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.project = codebuild.Project(
            self,
            "Project",
            project_name=warmup_project(prefix),
            description=f"FAST Channels ({prefix}): synthetic probe auctions that warm new Prebid Server tasks",
            build_spec=codebuild.BuildSpec.from_object(
                warmup_buildspec(
                    prefix=prefix,
                    cloudfront_domain=cloudfront_domain,
                    cluster_arn=cluster_arn,
                    target_group_arn=target_group_arn,
                )
            ),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
                compute_type=codebuild.ComputeType.SMALL,
            ),
            timeout=Duration.minutes(20),
            logging=codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=log_group)),
        )
        allow(
            self.project,
            ["secretsmanager:GetSecretValue"],
            [
                stack.format_arn(
                    service="secretsmanager",
                    region=EDGE_REGION,
                    resource="secret",
                    resource_name=f"{origin_secret(prefix, 'a')}-*",
                    arn_format=ArnFormat.COLON_RESOURCE_NAME,
                )
            ],
            why="Origin secret A; Secrets Manager appends a random suffix to the name",
        )
        cluster_name = cluster_arn.rsplit("/", 1)[1]
        allow(
            self.project,
            ["ecs:DescribeTasks"],
            [stack.format_arn(service="ecs", resource="task", resource_name=f"{cluster_name}/*")],
            why="The new task's address, to find it among the load balancer's targets",
        )
        allow(
            self.project,
            ["elasticloadbalancing:DescribeTargetHealth"],
            ["*"],
            why="DescribeTargetHealth supports no resource-level permissions",
        )
        nag.acknowledge(
            self.project, {"AwsSolutions-CB4": "No artifacts, only a synthetic replay: the AWS managed key suffices"}
        )
        nag.acknowledge_references(
            self.project,
            {
                nag.wildcard_resource(
                    f"arn:{stack.partition}:logs:{stack.region}:{stack.account}:log-group:/aws/codebuild/"
                    f"{self.project.project_name}:*"
                ): "CodeBuild's default log group grant (the project logs to its own log group)",
                nag.wildcard_resource(
                    f"arn:{stack.partition}:codebuild:{stack.region}:{stack.account}:report-group/"
                    f"{self.project.project_name}-*"
                ): "CodeBuild's default report-group grant (the build writes no reports)",
            },
        )
        self.rule = events.Rule(
            self,
            "NewTask",
            description=f"FAST Channels ({prefix}): warm every new Prebid Server task",
            event_pattern=events.EventPattern(
                source=["aws.ecs"],
                detail_type=["ECS Task State Change"],
                detail={
                    "clusterArn": [cluster_arn],
                    "group": [f"service:{service_arn.rsplit('/', 1)[1]}"],
                    "lastStatus": ["RUNNING"],
                    "desiredStatus": ["RUNNING"],
                },
            ),
            targets=[
                events_targets.CodeBuildProject(
                    self.project,
                    retry_attempts=2,
                    dead_letter_queue=dead_letters,
                    event=events.RuleTargetInput.from_object(
                        {
                            "environmentVariablesOverride": [
                                {
                                    "name": "TASK_ARN",
                                    "value": events.EventField.from_path("$.detail.taskArn"),
                                    "type": "PLAINTEXT",
                                }
                            ]
                        }
                    ),
                )
            ],
        )
