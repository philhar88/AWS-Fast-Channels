import aws_cdk as cdk
from aws_cdk import aws_lambda as lambda_

from fast_channels.infra.bundling import workspace_code

app = cdk.App()
stack = cdk.Stack(app, "Spike")
lambda_.Function(stack, "Fn", runtime=lambda_.Runtime.PYTHON_3_14, architecture=lambda_.Architecture.ARM_64,
                 handler="fast_channels.functions.spike.handler", code=workspace_code("fast-channels-functions"))
app.synth()
