"""ARNs of a deployment's resources, for IAM and resource policies."""

from typing import TYPE_CHECKING

from aws_cdk import ArnFormat, Fn, Stack

if TYPE_CHECKING:
    from aws_cdk import aws_stepfunctions as sfn
    from constructs import IConstruct


def arn(scope: IConstruct, service: str, resource: str, name: str, *, colon: bool = False) -> str:
    """``arn:<partition>:<service>:<region>:<account>:<resource>/<name>`` in ``scope``'s stack, or
    ``...:<resource>:<name>`` with ``colon`` (log groups, delivery sources, executions).

    The account and region are literals where the stack's environment is concrete and pseudo parameters otherwise,
    as with ``Stack.format_arn``; ``name`` may hold wildcards or tokens.
    """
    return Stack.of(scope).format_arn(
        service=service,
        resource=resource,
        resource_name=name,
        arn_format=ArnFormat.COLON_RESOURCE_NAME if colon else ArnFormat.SLASH_RESOURCE_NAME,
    )


def executions(state_machine: sfn.IStateMachine) -> str:
    """``arn:...:execution:<state machine name>``, as the CDK Step Functions grants write it (append ``*`` for its
    executions)."""
    return arn(
        state_machine, "states", "execution", Fn.select(6, Fn.split(":", state_machine.state_machine_arn)), colon=True
    )
