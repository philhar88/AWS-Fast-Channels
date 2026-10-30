"""``ImageBuild``: the stage's Prebid Server image, built on x86 CodeBuild from the guidance's Dockerfile.

The development host and the CI runners are arm64; the image is x86, and its jlinked Java runtime is
architecture-specific, so it is built in AWS. ``scripts/prebid_guidance.py build-image`` uploads the build context
(the guidance's ``deployment/ecr/prebid-server`` with its ``amt-bidder`` sources, overlays and the fast-ctv module) to
the stage's dedicated bootstrap bucket under ``pbs-image/``, starts the build with it and waits.
``build-image.sh`` refuses a Prebid Server Java tag that no longer resolves to its pinned commit, then runs
``docker build`` with ``INCLUDE_AMT_BIDDER=true`` and pushes ``<repository>:<tag>``; tags are content hashes, so
they are immutable.

With the PDS module on it also holds the PDS sidecar's repository (``<prefix>-pds``): the same project builds that
image from a context of its own (``pds/build-image.sh``), naming the repository in ``REPOSITORY_URI``.
"""

from typing import Final

from aws_cdk import Aws, Duration, RemovalPolicy, Stack
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from constructs import Construct

from fast_channels.core.demand import IMAGE_SOURCE_PREFIX, assets_bucket, image_build_project, image_repository
from fast_channels.core.pds import names as pds_names
from fast_channels.infra import nag

KEEP_IMAGES = 10
BUILD_SPEC: Final = {"version": "0.2", "phases": {"build": {"commands": ["bash build-image.sh"]}}}
"""Each build is started with its context as an S3 source override (the project has none of its own: CodeBuild
refuses a project whose S3 source does not exist yet, and the bootstrap bucket comes after this stack)."""


class ImageBuild(Construct):
    """The image repository and the x86 build project.

    Args:
        prefix: deployment prefix (names).
        removal_policy: of the repositories (with ``DESTROY`` their images go with them).
        log_retention: of the build logs.
        pds: also the PDS sidecar's repository (``modules.pds.enabled``).

    Attributes:
        repository: the ECR repository ``<prefix>-pbs``.
        pds_repository: the ECR repository ``<prefix>-pds``, or None.
        project: the CodeBuild project ``<prefix>-pbs-image``.
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        removal_policy: RemovalPolicy,
        log_retention: logs.RetentionDays,
        pds: bool = False,
    ) -> None:
        super().__init__(scope, id)

        def repository(id: str, name: str) -> ecr.Repository:
            return ecr.Repository(
                self,
                id,
                repository_name=name,
                image_scan_on_push=True,
                image_tag_mutability=ecr.TagMutability.IMMUTABLE,
                lifecycle_rules=[ecr.LifecycleRule(description="Keep the newest images", max_image_count=KEEP_IMAGES)],
                removal_policy=removal_policy,
                empty_on_delete=removal_policy == RemovalPolicy.DESTROY,
            )

        self.repository = repository("Repository", image_repository(prefix))
        self.pds_repository = repository("PdsRepository", pds_names.image_repository(prefix)) if pds else None
        sources = s3.Bucket.from_bucket_name(self, "Sources", assets_bucket(prefix, Aws.ACCOUNT_ID, Aws.REGION))
        log_group = logs.LogGroup(self, "Logs", retention=log_retention, removal_policy=removal_policy)
        self.project = codebuild.Project(
            self,
            "Project",
            project_name=image_build_project(prefix),
            description="FAST Channels: the Prebid Server image (the guidance's Dockerfile with overlays), x86",
            build_spec=codebuild.BuildSpec.from_object(BUILD_SPEC),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                compute_type=codebuild.ComputeType.LARGE,
                privileged=True,
                environment_variables={
                    "REPOSITORY_URI": codebuild.BuildEnvironmentVariable(value=self.repository.repository_uri),
                },
            ),
            timeout=Duration.minutes(60),
            logging=codebuild.LoggingOptions(cloud_watch=codebuild.CloudWatchLoggingOptions(log_group=log_group)),
        )
        self.repository.grant_pull_push(self.project)
        if self.pds_repository is not None:
            self.pds_repository.grant_pull_push(self.project)
        sources.grant_read(self.project, f"{IMAGE_SOURCE_PREFIX}*")
        stack = Stack.of(self)
        nag.acknowledge(
            self.project,
            {
                "AwsSolutions-CB3": "Privileged mode runs the Docker daemon that builds the Prebid Server image",
                "AwsSolutions-CB4": "No build artifacts: the image goes to ECR, the logs to CloudWatch Logs, and the "
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


WHY_SOURCES = "The build reads only its own contexts under pbs-image/ in the stage's bootstrap bucket"
