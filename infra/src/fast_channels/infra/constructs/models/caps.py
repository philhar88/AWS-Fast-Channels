"""Spend caps of model-registry tasks (OD-03 circuit breakers): the token-counter table, its grant, and the optional
AWS Budgets action per task role."""

from decimal import Decimal
from typing import Final

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from constructs import Construct, IConstruct

from fast_channels.infra import nag
from fast_channels.infra.arns import arn
from fast_channels.infra.constructs.models.profiles import INVOKE_ACTIONS, TASK_TAG

COUNTER_PREFIX: Final = "COUNTER#"
"""Counter items are ``COUNTER#<task>#<UTC day>`` (``fast_channels.agent.caps``)."""
COUNTER_ACTIONS: Final = ("dynamodb:UpdateItem",)
"""A task's caller only counts: a conditional ``ADD`` before each run and call, an ``ADD`` of each call's cost."""


def counter_table_name(prefix: str) -> str:
    """``<prefix>-models``: the deployment's token counters."""
    return f"{prefix}-models"


class SpendCounters(dynamodb.TableV2):
    """``<prefix>-models``: one item per task and UTC day (``pk`` = ``COUNTER#<task>#<day>``) holding the day's
    spend, run and call counts and when the cap notification went out; items expire after 35 days (``expires_at``).
    On demand. No point-in-time recovery: a lost counter only forgets part of one day's spend."""

    def __init__(self, scope: Construct, id: str, *, prefix: str, removal_policy: RemovalPolicy) -> None:
        super().__init__(
            scope,
            id,
            table_name=counter_table_name(prefix),
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing=dynamodb.Billing.on_demand(),
            time_to_live_attribute="expires_at",
            removal_policy=removal_policy,
        )
        nag.acknowledge(
            self,
            {
                "AwsSolutions-DDB3": "Daily spend counters of a circuit breaker: a restore has no value, and "
                "a lost item only forgets part of one day's spend"
            },
        )


def grant_counting(role: iam.IGrantable, scope: IConstruct, *, prefix: str, task: str) -> iam.Grant:
    """Let ``role`` count ``task``'s spend and nothing else: ``UpdateItem`` on the counter table, only on items whose
    key starts with ``COUNTER#<task>#``. The table is addressed by name, so ``scope``'s stack takes no reference to
    the Models stack."""
    return iam.Grant.add_to_principal(
        grantee=role,
        actions=list(COUNTER_ACTIONS),
        resource_arns=[arn(scope, "dynamodb", "table", counter_table_name(prefix))],
        conditions={"ForAllValues:StringLike": {"dynamodb:LeadingKeys": [f"{COUNTER_PREFIX}{task}#*"]}},
    )


class TaskBudget(Construct):
    """An AWS Budget of one task's daily model spend whose action, once the spend reaches the cap, attaches a policy
    that denies the task role's invocations of the task's profile: a runaway task stops only itself.

    Budgets see a task's spend through its profile's ``fast:task`` cost allocation tag, which must be activated in
    the payer account (Billing console, Cost allocation tags). Their data lags by hours, so the token counter stays
    the fast guard; the budget catches what the counter cannot see, such as another caller of the profile. The
    action is not reversed by itself: reverse it in AWS Budgets (Action history) once the cause is fixed.

    Args:
        budget_name: unique in the account.
        task: the task (its ``fast:task`` tag value).
        daily_cap_usd: the budget, in US dollars per day.
        role: the task's role (its name is what the action attaches the deny policy to).
        profile_arn: the task's application inference profile.
        email: who is told when the action runs.

    Attributes:
        budget, action: the L1 resources; deny: the policy the action attaches; execution_role: the role AWS Budgets
        assumes to attach it (only to ``role``, only that policy).
    """

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        budget_name: str,
        task: str,
        daily_cap_usd: float,
        role: iam.IRole,
        profile_arn: str,
        email: str,
    ) -> None:
        super().__init__(scope, id)
        stack = Stack.of(self)
        self.deny = iam.ManagedPolicy(
            self,
            "Deny",
            description=f"Budget action of model task {task}: denies invoking its inference profile",
            statements=[
                iam.PolicyStatement(effect=iam.Effect.DENY, actions=list(INVOKE_ACTIONS), resources=[profile_arn])
            ],
        )
        self.execution_role = iam.Role(
            self,
            "Execution",
            description=f"AWS Budgets attaches the deny policy of model task {task} to its role",
            assumed_by=iam.ServicePrincipal(
                "budgets.amazonaws.com", conditions={"StringEquals": {"aws:SourceAccount": stack.account}}
            ),
        )
        self.execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:AttachRolePolicy", "iam:DetachRolePolicy"],
                resources=[role.role_arn],
                conditions={"ArnEquals": {"iam:PolicyARN": self.deny.managed_policy_arn}},
            )
        )
        self.budget = budgets.CfnBudget(
            self,
            "Budget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name=budget_name,
                budget_type="COST",
                time_unit="DAILY",
                budget_limit=budgets.CfnBudget.SpendProperty(amount=float(Decimal(str(daily_cap_usd))), unit="USD"),
                filter_expression=budgets.CfnBudget.ExpressionProperty(
                    tags=budgets.CfnBudget.TagValuesProperty(key=TASK_TAG, values=[task], match_options=["EQUALS"])
                ),
            ),
        )
        self.action = budgets.CfnBudgetsAction(
            self,
            "Action",
            budget_name=budget_name,
            action_type="APPLY_IAM_POLICY",
            action_threshold=budgets.CfnBudgetsAction.ActionThresholdProperty(type="PERCENTAGE", value=100),
            definition=budgets.CfnBudgetsAction.DefinitionProperty(
                iam_action_definition=budgets.CfnBudgetsAction.IamActionDefinitionProperty(
                    policy_arn=self.deny.managed_policy_arn, roles=[role.role_name]
                )
            ),
            execution_role_arn=self.execution_role.role_arn,
            notification_type="ACTUAL",
            approval_model="AUTOMATIC",
            subscribers=[budgets.CfnBudgetsAction.SubscriberProperty(type="EMAIL", address=email)],
        )
        self.action.add_dependency(self.budget)
