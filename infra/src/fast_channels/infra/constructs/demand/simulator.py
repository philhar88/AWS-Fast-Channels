"""``LambdaSimulator``: the guidance's bidder simulator as a Lambda target of an internal load balancer in the
guidance's VPC (``modules.demand.simulator: lambda``), at ``sim.<prefix>.fast.internal``.

The same ``handler.py`` as the guidance's simulator stack, with overlay O5 and the stage's demo creatives
(``video_creatives.json``), which ``scripts/prebid_guidance.py`` packages into the guidance's assets bucket. It needs no
VPC of its own, no NAT gateway and no peering: only Prebid Server's tasks reach it, over the VPC.
"""

from typing import Final

from aws_cdk import Annotations, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as route53_targets
from aws_cdk import aws_s3 as s3
from constructs import Construct

from fast_channels.core.demand import assets_bucket, simulator_function
from fast_channels.core.pbs_config import SIMULATOR_RECORD
from fast_channels.infra import nag
from fast_channels.infra.constructs.lambda_target import ScopedLambdaTarget

SIMULATOR_ENVIRONMENT: Final = {
    "BID_RESPONSES_DELAY_PERCENTAGE": "0",
    "BID_RESPONSES_TIMEOUT_PERCENTAGE": "0",
    "A_BID_RESPONSE_DELAY_PROBABILITY": "0",
    "A_BID_RESPONSE_TIMEOUT_PROBABILITY": "0",
}
"""The handler's simulated delays and timeouts, off: the demo's bids answer at once."""
WHY: Final = "The demo's bidder simulator: simulated bids for an internal load balancer only Prebid Server reaches"


class LambdaSimulator(Construct):
    """The simulator function, its internal load balancer and its record.

    Args:
        prefix: the stage's prefix.
        vpc: the guidance's VPC.
        task_security_group_id: the Prebid Server tasks' security group, the only source admitted.
        code_key: the code package's key in the guidance's assets bucket.
        zone: the stage's private zone.
        log_retention: the function's log retention.

    Attributes:
        function: the simulator.
        load_balancer: the internal load balancer (HTTP 80).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        prefix: str,
        vpc: ec2.IVpc,
        task_security_group_id: str,
        code_key: str,
        zone: route53.IHostedZone,
        log_retention: logs.RetentionDays,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        code = s3.Bucket.from_bucket_name(self, "Code", assets_bucket(prefix, stack.account, stack.region))
        log_group = logs.LogGroup(
            self,
            "Logs",
            log_group_name=f"/aws/lambda/{simulator_function(prefix)}",
            retention=log_retention,
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Only its own log group: no managed policy (late log deliveries cannot re-create the group).
        role = iam.Role(
            self,
            "Role",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description=f"FAST Channels ({prefix}): the demo's bidder simulator",
        )
        log_group.grant_write(role)
        self.function = lambda_.Function(
            self,
            "Function",
            role=role,
            function_name=simulator_function(prefix),
            description="FAST Channels: the guidance's bidder simulator with the stage's demo creatives (simulated)",
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_bucket(code, code_key),
            memory_size=256,
            timeout=Duration.seconds(5),
            reserved_concurrent_executions=20,
            environment=SIMULATOR_ENVIRONMENT,
            log_group=log_group,
        )
        security_group = ec2.SecurityGroup(
            self,
            "LoadBalancerSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="FAST Channels: the bidder simulator load balancer, from Prebid Server tasks only",
        )
        tasks = ec2.SecurityGroup.from_security_group_id(self, "Tasks", task_security_group_id, mutable=False)
        security_group.add_ingress_rule(tasks, ec2.Port.tcp(80), "Prebid Server tasks")
        self.load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=vpc,
            internet_facing=False,
            security_group=security_group,
            vpc_subnets=ec2.SubnetSelection(subnets=vpc.private_subnets),
            drop_invalid_header_fields=True,
        )
        listener = self.load_balancer.add_listener("Http", port=80, open=False)
        listener.add_targets(  # the target group shares the function's name
            "Simulator",
            target_group_name=simulator_function(prefix),
            targets=[ScopedLambdaTarget(self.function, simulator_function(prefix))],
        )
        route53.ARecord(
            self,
            "Record",
            zone=zone,
            record_name=SIMULATOR_RECORD,
            target=route53.RecordTarget.from_alias(route53_targets.LoadBalancerTarget(self.load_balancer)),
        )
        nag.acknowledge(self.load_balancer, {"AwsSolutions-ELB2": f"{WHY}; the function logs every request"})
        nag.acknowledge(self.function, {nag.LAMBDA_DLQ: f"{WHY}; invoked synchronously by the load balancer"})
        nag.acknowledge_references(
            role,
            {nag.wildcard_resource(): "Active tracing: xray:PutTraceSegments/PutTelemetryRecords only support '*'"},
        )
        # The key names the package's content hash (``simulator/<hash>.zip``): new code is a new key, so the
        # function updates without an object version.
        Annotations.of(code).acknowledge_warning(
            "@aws-cdk/aws-lambda:codeFromBucketObjectVersionNotSpecified",
            "The code key is the package's content hash, so changed code is a new key",
        )
