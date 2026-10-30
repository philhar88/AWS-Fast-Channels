"""A load balancer's Lambda target whose invoke permission names the target group that may use it.

CDK's ``LambdaTarget`` grants ``elasticloadbalancing.amazonaws.com`` invoke without a condition, so any account's
target group could register the function and invoke it: the confused-deputy pattern. :class:`ScopedLambdaTarget`
adds ``SourceAccount`` and a ``SourceArn`` instead.

Elastic Load Balancing checks the permission when it registers the target, and CloudFormation registers the target
when it creates the target group, so the permission must exist first and cannot refer to the group's ARN (that would
be a cycle). The group therefore has a fixed name, and the permission matches ``targetgroup/<name>/*``: Lambda
compares ``SourceArn`` with ``ArnLike``.
"""

from typing import Final

import jsii
from aws_cdk import ArnFormat, Stack
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_

PRINCIPAL: Final = "elasticloadbalancing.amazonaws.com"


@jsii.implements(elbv2.IApplicationLoadBalancerTarget)
class ScopedLambdaTarget:
    """``function`` as the target of the application target group named ``target_group_name`` (module docstring)."""

    def __init__(self, function: lambda_.Function | lambda_.Alias, target_group_name: str) -> None:
        self.function = function
        self.target_group_name = target_group_name

    def attach_to_application_target_group(
        self, target_group: elbv2.IApplicationTargetGroup
    ) -> elbv2.LoadBalancerTargetProps:
        stack = Stack.of(target_group)
        permission = lambda_.CfnPermission(
            self.function,
            f"InvokeFrom{target_group.node.id}",
            action="lambda:InvokeFunction",
            function_name=self.function.function_arn,
            principal=iam.ServicePrincipal(PRINCIPAL).service,
            source_account=stack.account,
            source_arn=stack.format_arn(
                service="elasticloadbalancing",
                resource="targetgroup",
                resource_name=f"{self.target_group_name}/*",
                arn_format=ArnFormat.SLASH_RESOURCE_NAME,
            ),
        )
        target_group.node.add_dependency(permission)
        return elbv2.LoadBalancerTargetProps(
            target_type=elbv2.TargetType.LAMBDA, target_json={"id": self.function.function_arn}
        )
