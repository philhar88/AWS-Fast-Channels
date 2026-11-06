"""``Seller``: the Prebid Sales Agent as the stage's AdCP seller of record, in the guidance's VPC.

What Phase 0 ran (P0-13), as CDK: a Fargate service (arm64, 1 vCPU, 2 GB) on RDS for PostgreSQL 17 (``db.t4g.micro``,
no backups: a sandbox), behind an internal load balancer that CloudFront reaches through a VPC origin, so the seller
has HTTPS on ``*.cloudfront.net`` and no public address. The task has two containers of one image:

- ``App``: the seller, unmodified (MCP on ``/mcp/``, A2A 1.0 on ``/a2a``), in its testing mode, multi-tenant;
- ``Bridge``: the repository's bridge (``vendor/agentic-selling/seller/fast/bridge.py``), which seeds the tenant,
  keeps its products and exchanges buys and delivery with the sync function.

Workarounds of known seller issues, each to remove when upstream fixes it (docs/selling.md#workarounds-to-remove):

- ``OPTIMIZATION(oss): W-8 A7``: ``ADCP_MULTI_TENANT``, because single-tenant mode blocks ``create_media_buy``
  until SSO is configured;
- ``OPTIMIZATION(oss): W-8 A8``: CloudFront sends ``Fly-Forwarded-Proto: https``, which the image's nginx prefers,
  because the load balancer overwrites ``X-Forwarded-Proto`` to ``http`` and the seller would sign for ``http://``;
- ``OPTIMIZATION(oss): W-8 A9``: ``CREATIVE_AGENT_URL`` names a host that cannot resolve, because even in testing
  mode the seller dials the public reference creative agent (``creative.adcontextprotocol.org``) for previews;
- ``OPTIMIZATION(oss): W-8 A12``: the database password is drawn from characters that need no URL encoding, because
  the seller does not decode the password in ``DATABASE_URL``.
"""

import string
from typing import Final

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_rds as rds
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from fast_channels.core.selling.names import BUYER_PRINCIPAL, SELLER_PORT, TENANT, seller
from fast_channels.infra import nag

DATABASE: Final = "salesagent"
HEALTH: Final = "/health"
START_PERIOD: Final = Duration.minutes(5)
"""Migrations on an empty database, then nginx and the servers (P0-13: about 45 s; upstream allows 300 s)."""
PASSWORD_EXCLUDE: Final = " " + "".join(c for c in string.punctuation if c not in "-._")
"""Characters left out of the generated database password, so it is letters, digits and ``-._``: characters a URL
carries as they are. ``OPTIMIZATION(oss): W-8 A12``: the seller reads the password from ``DATABASE_URL`` with
``urlparse(...).password``, which does not percent-decode, so a password that needs encoding (``^`` as ``%5E``) fails
to log in and the seller exits at its first database check."""


class Seller(Construct):
    """The seller's service, database, load balancer and CloudFront distribution.

    Args:
        prefix: deployment prefix (names).
        vpc: the guidance's VPC (private subnets with a NAT gateway).
        repository: the seller image's repository; ``tag``: its tag.
        sync_function: the sync function the bridge invokes.
        origin_facing_prefix_list: CloudFront's origin-facing managed prefix list.
        publisher_domain: what the tenant's property names.
        log_retention: of the containers' logs.

    Attributes:
        distribution: the seller's CloudFront distribution (its host is the tenant's virtual host).
        service: the Fargate service. buyer_token: the reference buyer's bearer token (a generated secret).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        vpc: ec2.IVpc,
        repository: ecr.IRepository,
        tag: str,
        sync_function: lambda_.IFunction,
        origin_facing_prefix_list: str,
        publisher_domain: str,
        log_retention: logs.RetentionDays,
    ) -> None:
        super().__init__(scope, id)
        name = seller(prefix)
        private = ec2.SubnetSelection(subnets=vpc.private_subnets)
        self.database = rds.DatabaseInstance(
            self,
            "Database",
            instance_identifier=name,
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.VER_17),
            instance_type=ec2.InstanceType.of(ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO),
            vpc=vpc,
            vpc_subnets=private,
            # OPTIMIZATION(oss): W-8 A12. Unnamed: a change of the characters replaces the secret, which a fixed name
            # would refuse on a deployed stack.
            credentials=rds.Credentials.from_generated_secret(DATABASE, exclude_characters=PASSWORD_EXCLUDE),
            database_name=DATABASE,
            allocated_storage=20,
            storage_type=rds.StorageType.GP3,
            storage_encrypted=True,
            multi_az=False,
            publicly_accessible=False,
            backup_retention=Duration.days(0),
            delete_automated_backups=True,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
        )
        assert self.database.secret is not None  # generated
        self.seed = secretsmanager.Secret(
            self,
            "Seed",
            secret_name=f"{name}-seed",
            description="Seed of the seller's own keys (Fernet, Flask, signing-key KEK), derived in its container",
            generate_secret_string=secretsmanager.SecretStringGenerator(password_length=64, exclude_punctuation=True),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.buyer_token = secretsmanager.Secret(
            self,
            "BuyerToken",
            secret_name=f"{name}-buyer-token",
            description="Bearer token of the seller's principal for the AWS reference buyer",
            generate_secret_string=secretsmanager.SecretStringGenerator(password_length=48, exclude_punctuation=True),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.cluster = cluster = ecs.Cluster(
            self, "Cluster", cluster_name=name, vpc=vpc, container_insights_v2=ecs.ContainerInsights.DISABLED
        )
        task = ecs.FargateTaskDefinition(
            self,
            "Task",
            family=name,
            cpu=1024,
            memory_limit_mib=2048,  # the seller idles at about 1.25 GB; 1 GB is too small (upstream)
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64, operating_system_family=ecs.OperatingSystemFamily.LINUX
            ),
        )
        log_group = logs.LogGroup(
            self, "Logs", log_group_name=name, retention=log_retention, removal_policy=RemovalPolicy.DESTROY
        )
        self.distribution = self._distribution(prefix, vpc, private, origin_facing_prefix_list)
        image = ecs.ContainerImage.from_ecr_repository(repository, tag)
        environment = {
            "ENVIRONMENT": "production",
            "ADCP_TESTING": "true",  # a sandbox: simulated money, the reference format catalog, seeded delivery
            "ADCP_MULTI_TENANT": "true",  # OPTIMIZATION(oss): W-8 A7
            "SALES_AGENT_DOMAIN": f"{prefix}.fast-channels.invalid",  # every tenant is a custom-host tenant
            # OPTIMIZATION(oss): W-8 A9
            "CREATIVE_AGENT_URL": f"https://creative-agent.{prefix}.fast-channels.invalid",
            "SKIP_CRON": "true",
            "SUPPORT_EMAIL": f"operator@{publisher_domain}",
            "DB_HOST": self.database.db_instance_endpoint_address,
            "DB_PORT": self.database.db_instance_endpoint_port,
            "DB_NAME": DATABASE,
            "DB_USER": DATABASE,
            "DB_SSLMODE": "require",
            "AWS_REGION": Stack.of(self).region,
            "AWS_DEFAULT_REGION": Stack.of(self).region,
        }
        database = self.database.secret
        secrets = {
            "DB_PASSWORD": ecs.Secret.from_secrets_manager(database, "password"),
            "FAST_SECRET_SEED": ecs.Secret.from_secrets_manager(self.seed),
        }
        app = task.add_container(
            "App",
            container_name="app",
            image=image,
            command=["serve"],
            essential=True,
            port_mappings=[ecs.PortMapping(container_port=SELLER_PORT)],
            environment=environment,
            secrets=secrets,
            health_check=ecs.HealthCheck(
                command=["CMD-SHELL", f"curl -fsS http://localhost:{SELLER_PORT}{HEALTH} || exit 1"],
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                retries=3,
                start_period=START_PERIOD,
            ),
            logging=ecs.LogDrivers.aws_logs(stream_prefix="app", log_group=log_group),
        )
        bridge = task.add_container(
            "Bridge",
            container_name="bridge",
            image=image,
            command=["bridge"],
            essential=False,  # the seller keeps serving while the bridge restarts
            environment={
                **environment,
                "FAST_TENANT": TENANT,
                "FAST_SELLER_HOST": self.distribution.distribution_domain_name,
                "FAST_BUYER_PRINCIPAL": BUYER_PRINCIPAL,
                "FAST_PUBLISHER_DOMAIN": publisher_domain,
                "FAST_SYNC_FUNCTION": sync_function.function_name,
            },
            secrets={**secrets, "FAST_BUYER_TOKEN": ecs.Secret.from_secrets_manager(self.buyer_token)},
            logging=ecs.LogDrivers.aws_logs(stream_prefix="bridge", log_group=log_group),
            enable_restart_policy=True,
            restart_attempt_period=Duration.seconds(60),
        )
        bridge.add_container_dependencies(
            ecs.ContainerDependency(container=app, condition=ecs.ContainerDependencyCondition.HEALTHY)
        )
        sync_function.grant_invoke(task.task_role)
        nag.acknowledge_references(
            task.task_role,
            {
                nag.wildcard_resource(
                    f"{sync_function.function_arn}:*"
                ): "lambda grant_invoke: the sync function's versions"
            },
        )
        self.service = ecs.FargateService(
            self,
            "Service",
            service_name=name,
            cluster=cluster,
            task_definition=task,
            desired_count=1,
            # Stop first, as upstream: two tasks must never run the migrations at once.
            min_healthy_percent=0,
            max_healthy_percent=100,
            vpc_subnets=private,
            assign_public_ip=False,
            health_check_grace_period=START_PERIOD,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=False),
        )
        self.database.connections.allow_default_port_from(self.service, "The seller tasks")
        self.listener.add_targets(
            "Seller",
            port=SELLER_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[self.service.load_balancer_target(container_name="app", container_port=SELLER_PORT)],
            health_check=elbv2.HealthCheck(path=HEALTH, interval=Duration.seconds(15), healthy_threshold_count=2),
            deregistration_delay=Duration.seconds(10),
        )
        self._acknowledge(task)

    def _distribution(
        self, prefix: str, vpc: ec2.IVpc, private: ec2.SubnetSelection, prefix_list: str
    ) -> cloudfront.Distribution:
        """The internal load balancer and the CloudFront distribution that reaches it through a VPC origin."""
        self.load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            load_balancer_name=seller(prefix),
            vpc=vpc,
            vpc_subnets=private,
            internet_facing=False,
            idle_timeout=Duration.seconds(300),
            drop_invalid_header_fields=True,
        )
        self.load_balancer.connections.allow_from(
            ec2.Peer.prefix_list(prefix_list), ec2.Port.tcp(80), "CloudFront VPC origin (origin-facing prefix list)"
        )
        self.listener = self.load_balancer.add_listener("Http", port=80, open=False)
        return cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"{seller(prefix)}: the AdCP seller (MCP /mcp/, A2A /a2a)",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.VpcOrigin.with_application_load_balancer(
                    self.load_balancer,
                    http_port=80,
                    protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
                    read_timeout=Duration.seconds(60),
                    # OPTIMIZATION(oss): W-8 A8
                    custom_headers={"Fly-Forwarded-Proto": "https"},
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
                compress=False,
            ),
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            enable_ipv6=False,
        )

    def _acknowledge(self, task: ecs.FargateTaskDefinition) -> None:
        sandbox = "A sandbox seller (testing mode, simulated money) for one stage, destroyed with it"
        nag.acknowledge(
            self.database,
            {
                "AwsSolutions-RDS2": "Storage is encrypted (storage_encrypted)",
                "AwsSolutions-RDS3": f"{sandbox}: one Availability Zone",
                "AwsSolutions-RDS6": "The seller connects with its database password (upstream supports no IAM auth)",
                "AwsSolutions-RDS10": f"{sandbox}: no deletion protection",
                "AwsSolutions-RDS11": "Reachable only from the seller's tasks' security group",
                "AwsSolutions-RDS13": f"{sandbox}: no backups",
                "AwsSolutions-SMG4": f"{sandbox}: the generated password is not rotated",
            },
        )
        for secret in (self.seed, self.buyer_token):
            nag.acknowledge(secret, {"AwsSolutions-SMG4": f"{sandbox}: a generated value read at start, not rotated"})
        nag.acknowledge(
            self.load_balancer,
            {
                "AwsSolutions-ELB2": "CloudFront in front of it logs every request; the seller logs each call",
                "AwsSolutions-EC23": "Admits only CloudFront's origin-facing prefix list",
            },
        )
        nag.acknowledge(
            self.distribution,
            {
                "AwsSolutions-CFR1": "AdCP buyer agents call from anywhere; the seller authenticates every spend call",
                "AwsSolutions-CFR2": f"{sandbox}: no web ACL",
                "AwsSolutions-CFR3": f"{sandbox}: the seller logs every call (its container log)",
                "AwsSolutions-CFR4": "The default *.cloudfront.net certificate, which allows TLSv1 at the viewer",
            },
        )
        nag.acknowledge(task, {"AwsSolutions-ECS2": "Plain settings only; the secrets come from Secrets Manager"})
        nag.acknowledge(self.cluster, {"AwsSolutions-ECS4": f"{sandbox}: one task; its containers log every call"})
        nag.acknowledge_references(
            task.obtain_execution_role(),
            {nag.wildcard_resource(): "ecr:GetAuthorizationToken has no resource-level permissions"},
        )
