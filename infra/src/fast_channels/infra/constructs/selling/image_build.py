"""``SellingImageBuild``: the agentic-selling module's images, built on arm64 CodeBuild from pinned upstream code.

AgentCore Runtime runs arm64 containers only, so the buyer and governance agents are arm64, and so is the seller, as
Phase 0 ran it. ``scripts/prebid_guidance.py deploy`` uploads each image's build context (``vendor/agentic-selling``)
to the stage's dedicated bootstrap bucket under ``pbs-image/``, starts the build with it and waits.
``build-image.sh`` clones the upstream repository at its pinned commit (``vendor/agentic-selling/PIN.toml``) and
builds it with the upstream Dockerfile; tags are content hashes, so they are immutable.
"""

from aws_cdk import Aws, Duration, RemovalPolicy, Stack
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from fast_channels.core.demand import IMAGE_SOURCE_PREFIX, assets_bucket
from fast_channels.core.selling.names import Image, image_build_project, image_repository
from fast_channels.infra import nag
from fast_channels.infra.constructs.demand.image_build import BUILD_SPEC, KEEP_IMAGES, WHY_SOURCES


class SellingImageBuild(Construct):
    """A repository per image and the arm64 build project.

    Args:
        prefix: deployment prefix (names).
        removal_policy: of the repositories (with ``DESTROY`` their images go with them).
        log_retention: of the build logs.

    Attributes:
        repositories: the ECR repository of each image (``<prefix>-seller``, ``-buyer``, ``-governance``).
        project: the CodeBuild project ``<prefix>-selling-image``.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        removal_policy: RemovalPolicy,
        log_retention: logs.RetentionDays,
    ) -> None:
        super().__init__(scope, id)
        self.repositories = {
            image: ecr.Repository(
                self,
                f"{image.value.capitalize()}Repository",
                repository_name=image_repository(prefix, image),
                image_scan_on_push=True,
                image_tag_mutability=ecr.TagMutability.IMMUTABLE,
                lifecycle_rules=[ecr.LifecycleRule(description="Keep the newest images", max_image_count=KEEP_IMAGES)],
                removal_policy=removal_policy,
                empty_on_delete=removal_policy == RemovalPolicy.DESTROY,
            )
            for image in Image
        }
        sources = s3.Bucket.from_bucket_name(self, "Sources", assets_bucket(prefix, Aws.ACCOUNT_ID, Aws.REGION))
        log_group = logs.LogGroup(self, "Logs", retention=log_retention, removal_policy=removal_policy)
        self.project = codebuild.Project(
            self,
            "Project",
            project_name=image_build_project(prefix),
            description="FAST Channels: the agentic-selling images (seller, buyer, governance) from pinned upstream "
            "code, arm64",
            build_spec=codebuild.BuildSpec.from_object(BUILD_SPEC),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
                compute_type=codebuild.ComputeType.LARGE,
                privileged=True,
            ),
            timeout=Duration.minutes(60),
            logging=codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=log_group)),
        )
        for repository in self.repositories.values():
            repository.grant_pull_push(self.project)
        sources.grant_read(self.project, f"{IMAGE_SOURCE_PREFIX}*")
        stack = Stack.of(self)
        nag.acknowledge(
            self.project,
            {
                "AwsSolutions-CB3": "Privileged mode runs the Docker daemon that builds the images",
                "AwsSolutions-CB4": "No build artifacts: the images go to ECR, the logs to CloudWatch Logs, and the "
                "sources are only read",
                nag.wildcard_action("s3:GetBucket*"): WHY_SOURCES,
                nag.wildcard_action("s3:GetObject*"): WHY_SOURCES,
                nag.wildcard_action("s3:List*"): WHY_SOURCES,
                nag.wildcard_resource(): "ecr:GetAuthorizationToken has no resource-level permissions",
            },
        )
        nag.acknowledge_references(
            self.project,
            {
                nag.wildcard_resource(f"{sources.bucket_arn}/{IMAGE_SOURCE_PREFIX}*"): WHY_SOURCES,
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
