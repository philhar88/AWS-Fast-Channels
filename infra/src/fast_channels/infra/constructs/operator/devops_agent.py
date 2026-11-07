"""AWS DevOps Agent, opt-in (``modules.agents.devops_agent``; M9, DESIGN-v2.2 §19): investigations and advice over
this deployment's account; it never acts.

- An Agent Space ``<prefix>-devops``, with elevated actions off (``ElevatedActionsEnabled`` left at its default,
  false) and no operator app: a human enables the DevOps Agent web app with their own identity provider when they
  want it.
- Its role, trusted by ``aidevops.amazonaws.com`` for this account's agent spaces only (``aws:SourceAccount``,
  ``aws:SourceArn``), with the AWS managed ``AIDevOpsAgentAccessPolicy`` (the reads an investigation makes) and the
  Resource Explorer service-linked role it creates, as the DevOps Agent onboarding guide sets it up. An explicit deny
  of ``support:CreateCase``, the policy's one write, keeps the agent to advice.
- An Association of this account as the ``monitor`` account with that role and no elevated role, so the agent
  discovers the topology and investigates, and has no role to act with.

Idle, it costs nothing; investigations, evaluations and on-demand tasks are billed per agent-second
(docs/agents.md#the-devops-agent). Its findings are for the people and the agents who read them: in v1 nothing feeds
them to the roster automatically.
"""

from typing import Final

from aws_cdk import Aws, Stack
from aws_cdk import aws_devopsagent as devopsagent
from aws_cdk import aws_iam as iam
from constructs import Construct

from fast_channels.infra import nag

SERVICE: Final = "aidevops.amazonaws.com"
ACCESS_POLICY: Final = "AIDevOpsAgentAccessPolicy"
RESOURCE_EXPLORER_SLR: Final = "aws-service-role/resource-explorer-2.amazonaws.com/AWSServiceRoleForResourceExplorer"


class DevOpsAgentSpace(Construct):
    """The Agent Space, its read-only role and the account's ``monitor`` association (module docstring).

    Attributes:
        space: the Agent Space. role: the role the agent assumes. association: this account's association.
    """

    def __init__(self, scope: Construct, id: str, *, prefix: str) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.role = iam.Role(
            self,
            "Role",
            description=f"AWS DevOps Agent ({prefix}): read-only investigations of this account",
            assumed_by=iam.ServicePrincipal(
                SERVICE,
                conditions={
                    "StringEquals": {"aws:SourceAccount": stack.account},
                    "ArnLike": {
                        "aws:SourceArn": f"arn:{Aws.PARTITION}:aidevops:{stack.region}:{stack.account}:agentspace/*"
                    },
                },
            ),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(ACCESS_POLICY)],
        )
        self.role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:CreateServiceLinkedRole"],
                resources=[f"arn:{Aws.PARTITION}:iam::{stack.account}:role/{RESOURCE_EXPLORER_SLR}"],
            )
        )
        self.role.add_to_policy(
            iam.PolicyStatement(effect=iam.Effect.DENY, actions=["support:CreateCase"], resources=["*"])
        )
        nag.acknowledge(
            self.role,
            {
                nag.managed_policy(
                    ACCESS_POLICY
                ): "The AWS managed policy the DevOps Agent onboarding guide attaches; AWS keeps "
                "it current with the reads an investigation needs",
            },
        )
        self.space = devopsagent.CfnAgentSpace(
            self,
            "Space",
            name=f"{prefix}-devops",
            description=f"FAST Channels ({prefix}): investigations and advice; it never acts",
        )
        self.association = devopsagent.CfnAssociation(
            self,
            "Account",
            agent_space_id=self.space.attr_agent_space_id,
            service_id="aws",
            configuration=devopsagent.CfnAssociation.ServiceConfigurationProperty(
                aws=devopsagent.CfnAssociation.AWSConfigurationProperty(
                    account_id=stack.account, account_type="monitor", assumable_role_arn=self.role.role_arn
                )
            ),
        )
        self.association.node.add_dependency(self.role)
