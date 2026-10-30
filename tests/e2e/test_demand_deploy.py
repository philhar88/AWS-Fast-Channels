"""Offline tests of the demand module's deploy and destroy (scripts/_demand.py, not marked e2e), with botocore
Stubber standing in for AWS. The live deploy, smoke and destroy run in the e2e demand leg."""

import io
import json
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import _demand
import boto3
import prebid_guidance
import pytest
from botocore.stub import Stubber

from fast_channels.core.demand import Parameter
from fast_channels.core.demo_demand import DEMO_CREATIVES, demo_index, demo_index_key
from fast_channels.core.ladder import Ladder
from fast_channels.core.specs import vast_key

REGION = "us-east-1"
LOCK = """
[[package]]
name = "aws-cdk-lib"
version = "2.237.0"
groups = ["main", "dev", "local"]
files = [{file = "aws_cdk_lib-2.237.0-py3-none-any.whl", hash = "sha256:aaa"}]

[[package]]
name = "pywin32"
version = "310"
groups = ["main"]
markers = "sys_platform == \\"win32\\""
files = [{file = "pywin32-310-cp311-cp311-win32.whl", hash = "sha256:bbb"}]

[[package]]
name = "pytest-mock"
version = "3.14.0"
groups = ["dev"]
markers = {dev = "python_version >= \\"3.11\\""}
files = [{file = "pytest_mock-3.14.0-py3-none-any.whl", hash = "sha256:ccc"}]

[[package]]
name = "sphinx"
version = "8.0.0"
groups = ["docs"]
files = [{file = "sphinx-8.0.0-py3-none-any.whl", hash = "sha256:ddd"}]

[[package]]
name = "aws-solutions-cdk"
version = "2.0.0"
groups = ["local"]
files = []

[package.source]
type = "directory"
url = "cdk_solution_helper_py/helpers_cdk"
"""


def session() -> boto3.Session:
    return boto3.Session(region_name=REGION, aws_access_key_id="test", aws_secret_access_key="test")


@pytest.fixture(scope="module")
def fastci() -> Any:
    return prebid_guidance.stage_config("fastci", "fastcix", REGION)


@pytest.fixture
def stage(fastci: Any) -> Iterator[_demand.DemandStage]:
    demand = _demand.DemandStage(fastci, "fastci", session())
    demand.__dict__["account"] = "123456789012"  # cached_property: no STS call
    yield demand


@pytest.fixture
def stub(stage: _demand.DemandStage) -> Iterator[Any]:
    stubbers: list[Stubber] = []

    def stubbed(service: str, region: str | None = None) -> Stubber:
        stubber = Stubber(stage.client(service, region))
        stubber.activate()
        stubbers.append(stubber)
        return stubber

    yield stubbed
    for stubber in stubbers:
        stubber.assert_no_pending_responses()
        stubber.deactivate()


def test_lock_requirements_keep_the_installed_groups_with_hashes_and_markers(tmp_path: Path) -> None:
    lock = tmp_path / "poetry.lock"
    lock.write_text(LOCK)

    requirements, local = _demand.lock_requirements(lock)

    assert requirements == [
        "aws-cdk-lib==2.237.0 --hash=sha256:aaa",
        'pywin32==310 ; sys_platform == "win32" --hash=sha256:bbb',
        'pytest-mock==3.14.0 ; (python_version >= "3.11") --hash=sha256:ccc',
    ]
    assert local == ["cdk_solution_helper_py/helpers_cdk"]


def test_the_image_tag_is_the_contexts_content(tmp_path: Path) -> None:
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "buildspec.yml").write_text("version: 0.2\n")
    first = _demand.context_digest(tmp_path)

    assert _demand.context_digest(tmp_path) == first
    assert _demand.zip_directory(tmp_path) == _demand.zip_directory(tmp_path)
    with zipfile.ZipFile(io.BytesIO(_demand.zip_directory(tmp_path))) as archive:
        assert sorted(archive.namelist()) == ["buildspec.yml", "context/Dockerfile"]
    (tmp_path / "context" / "Dockerfile").write_text("FROM alpine\n")
    assert _demand.context_digest(tmp_path) != first


def test_a_release_the_guidance_does_not_build_gets_its_own_build_config(
    stage: _demand.DemandStage, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(_demand, "BUILD", tmp_path)
    config = stage.checkout / _demand.CONFIG_ROOT / "docker-build-config.json"
    (stage.checkout / _demand.CONFIG_ROOT / "amt-bidder").mkdir(parents=True)
    (stage.checkout / "source/loadtest/amt-bidder").mkdir(parents=True)
    shipped = {"GIT_TAG_VERSION": "3.43.0", "GIT_REPO_URL": "https://github.com/prebid/prebid-server-java"}
    config.write_text(json.dumps(shipped | {"MVN_CLI_OPTIONS": "-Dmaven.test.skip"}))

    as_shipped = stage.build_context(tmp_path / "out")
    assert json.loads((as_shipped / "context/docker-build-config.json").read_text())["GIT_TAG_VERSION"] == "3.43.0"
    shipped_digest = _demand.context_digest(as_shipped)

    spec = prebid_guidance.PbsBuild(jdk="25", mvn_options="-Dmaven.test.skip -Dcheckstyle.skip")
    context = stage.build_context(tmp_path / "out", ("4.4.0", spec))
    assert json.loads((context / "context/docker-build-config.json").read_text()) == {
        "GIT_TAG_VERSION": "4.4.0",
        "GIT_REPO_URL": "https://github.com/prebid/prebid-server-java",
        "MVN_CLI_OPTIONS": "-Dmaven.test.skip -Dcheckstyle.skip",
    }
    assert json.loads(config.read_text())["GIT_TAG_VERSION"] == "3.43.0", "the checkout is unchanged"
    assert _demand.context_digest(context) != shipped_digest, "another image tag"


def test_running_tasks_without_a_warm_up_are_warmed(stage: _demand.DemandStage, stub: Any) -> None:
    ssm, ecs, codebuild = stub("ssm"), stub("ecs"), stub("codebuild")
    cluster = "arn:aws:ecs:us-east-1:123456789012:cluster/c"
    service = "arn:aws:ecs:us-east-1:123456789012:service/c/s"
    old, new = (f"arn:aws:ecs:us-east-1:123456789012:task/c/{t}" for t in ("old", "new"))
    page = {
        "Parameters": [
            {"Name": "/fastcix/demand/cluster-arn", "Value": cluster},
            {"Name": "/fastcix/demand/service-arn", "Value": service},
        ]
    }
    ssm.add_response("get_parameters_by_path", page, {"Path": "/fastcix/demand/"})
    ecs.add_response(
        "list_tasks", {"taskArns": [old, new]}, {"cluster": cluster, "serviceName": service, "desiredStatus": "RUNNING"}
    )
    project = "fastcix-pbs-warmup"
    codebuild.add_response(
        "list_builds_for_project", {"ids": [f"{project}:1"]}, {"projectName": project, "sortOrder": "DESCENDING"}
    )
    environment = {
        "type": "ARM_CONTAINER",
        "image": "i",
        "computeType": "BUILD_GENERAL1_SMALL",
        "environmentVariables": [{"name": "TASK_ARN", "value": old, "type": "PLAINTEXT"}],
    }
    codebuild.add_response(
        "batch_get_builds", {"builds": [{"id": f"{project}:1", "environment": environment}]}, {"ids": [f"{project}:1"]}
    )
    override = [{"name": "TASK_ARN", "value": new, "type": "PLAINTEXT"}]
    codebuild.add_response(
        "start_build",
        {"build": {"id": f"{project}:2"}},
        {"projectName": project, "environmentVariablesOverride": override},
    )
    codebuild.add_response(
        "batch_get_builds", {"builds": [{"id": f"{project}:2", "buildStatus": "SUCCEEDED"}]}, {"ids": [f"{project}:2"]}
    )

    assert stage.warm_running_tasks() == {new: "SUCCEEDED"}, "the task the rule already warmed is left alone"


def test_console_bucket() -> None:
    url = "https://us-east-1.console.aws.amazon.com/s3/home?region=us-east-1&bucket=fastcix-pbs-bucket83908e77-abc"

    assert _demand.console_bucket(url) == "fastcix-pbs-bucket83908e77-abc"


def test_retained_resources_include_what_lambda_and_container_insights_create() -> None:
    rows = [
        {"ResourceType": "AWS::S3::Bucket", "PhysicalResourceId": "fastcix-pbs-bucket"},
        {"ResourceType": "AWS::Logs::LogGroup", "PhysicalResourceId": "fastcix-pbs-container"},
        {"ResourceType": "AWS::Lambda::Function", "PhysicalResourceId": "fastcix-pbs-HeaderSecret"},
        {"ResourceType": "AWS::KMS::Key", "PhysicalResourceId": "key-1"},
        {"ResourceType": "AWS::ECS::TaskDefinition", "PhysicalResourceId": "arn:aws:ecs:td/x:1"},
        {"ResourceType": "AWS::EC2::VPC", "PhysicalResourceId": "vpc-1"},
        {"ResourceType": "AWS::S3::Bucket"},
    ]

    retained = _demand.retained_resources(rows, "fastcix-pbs-Cluster")

    assert retained == _demand.Retained(
        buckets=["fastcix-pbs-bucket"],
        log_groups=[
            "fastcix-pbs-container",
            "/aws/lambda/fastcix-pbs-HeaderSecret",
            "/aws/ecs/containerinsights/fastcix-pbs-Cluster/performance",
        ],
        keys=["key-1"],
        task_definitions=["arn:aws:ecs:td/x:1"],
    )


def test_names_follow_the_prefix(stage: _demand.DemandStage) -> None:
    assert (stage.prefix, stage.stage_id) == ("fastcix", "FastChannels-fastcix")
    assert stage.repository_stacks() == ["FastChannels-fastcix/DemandEdge", "FastChannels-fastcix/DemandBuild"]
    assert (stage.edge_stack, stage.build_stack) == (
        "FastChannels-fastcix-DemandEdge",
        "FastChannels-fastcix-DemandBuild",
    )
    assert stage.checkout.name == "fastcix"
    context = stage.repository_context()
    assert context[context.index("-c") + 1].startswith("fast-channels.")
    assert any(arg == 'fast-channels.prefix="fastcix"' for arg in context)


def test_the_pds_and_release_stacks_deploy_after_the_demand_stack(stage: _demand.DemandStage) -> None:
    assert (stage.pds_stack, stage.release_stack) == ("FastChannels-fastcix-Pds", "FastChannels-fastcix-Release")
    assert stage.beside_stacks() == [
        "FastChannels-fastcix/Demand",
        "FastChannels-fastcix/Pds",
        "FastChannels-fastcix/Release",
    ]


def _stack(name: str, outputs: dict[str, str]) -> dict[str, Any]:
    return {
        "StackName": name,
        "CreationTime": datetime(2026, 10, 29, tzinfo=UTC),
        "StackStatus": "CREATE_COMPLETE",
        "Outputs": [{"OutputKey": key, "OutputValue": value} for key, value in outputs.items()],
    }


PDS_PARAMETERS = ("config-bucket", "lake-bucket", "media-bucket", "content-base-url", "ladder-version")


def test_the_pds_needs_the_parameters_of_the_data_and_media_stacks(stage: _demand.DemandStage, stub: Any) -> None:
    ssm = stub("ssm")
    names = [f"/fastcix/pds/{p}" for p in (*PDS_PARAMETERS, "catalog-context-bucket")]  # fastci has the crawl module
    ssm.add_response(
        "get_parameters",
        {"Parameters": [], "InvalidParameters": names[3:]},
        {"Names": names},
    )
    with pytest.raises(_demand.DemandError, match=r"content-base-url.*make deploy"):
        stage.check_pds_state()

    ssm.add_response("get_parameters", {"Parameters": []}, {"Names": names})
    stage.check_pds_state()


def test_without_the_crawl_module_the_pds_needs_no_catalog_bucket(fastci: Any) -> None:
    modules = fastci.modules.model_copy(update={"crawl": fastci.modules.crawl.model_copy(update={"enabled": False})})
    stage = _demand.DemandStage(fastci.model_copy(update={"modules": modules}), "fastci", session())
    ssm = Stubber(stage.client("ssm"))
    names = [f"/fastcix/pds/{p}" for p in PDS_PARAMETERS]
    ssm.add_response("get_parameters", {"Parameters": []}, {"Names": names})
    with ssm:
        stage.check_pds_state()
    ssm.assert_no_pending_responses()


def test_a_pds_stack_the_module_no_longer_has_is_deleted(fastci: Any, stub: Any) -> None:
    modules = fastci.modules.model_copy(update={"pds": fastci.modules.pds.model_copy(update={"enabled": False})})
    stage = _demand.DemandStage(fastci.model_copy(update={"modules": modules}), "fastci", session())
    assert stage.beside_stacks() == ["FastChannels-fastcix/Demand", "FastChannels-fastcix/Release"]
    cloudformation = Stubber(stage.client("cloudformation"))
    name = {"StackName": "FastChannels-fastcix-Pds"}
    cloudformation.add_response("describe_stacks", {"Stacks": [_stack(name["StackName"], {})]}, name)
    cloudformation.add_response("delete_stack", {}, name)
    cloudformation.add_client_error("describe_stacks", "ValidationError", "Stack does not exist", expected_params=name)
    with cloudformation:
        stage.check_pds_state()
    cloudformation.assert_no_pending_responses()


def test_the_release_stack_needs_the_media_stacks_parameters(stage: _demand.DemandStage, stub: Any) -> None:
    ssm = stub("ssm")
    name = {"Name": "/fastcix/release/content-base-url"}
    ssm.add_client_error("get_parameter", "ParameterNotFound", expected_params=name)
    with pytest.raises(_demand.DemandError, match="make deploy"):
        stage.check_release_state()
    ssm.add_response("get_parameter", {"Parameter": {"Name": name["Name"], "Value": "https://cdn"}}, name)
    stage.check_release_state()


def test_a_release_stack_the_module_no_longer_has_is_deleted(fastci: Any) -> None:
    modules = fastci.modules.model_copy(
        update={"release": fastci.modules.release.model_copy(update={"enabled": False})}
    )
    stage = _demand.DemandStage(fastci.model_copy(update={"modules": modules}), "fastci", session())
    assert stage.beside_stacks() == ["FastChannels-fastcix/Demand", "FastChannels-fastcix/Pds"]
    cloudformation = Stubber(stage.client("cloudformation"))
    name = {"StackName": "FastChannels-fastcix-Release"}
    cloudformation.add_response("describe_stacks", {"Stacks": [_stack(name["StackName"], {})]}, name)
    cloudformation.add_response("delete_stack", {}, name)
    cloudformation.add_client_error("describe_stacks", "ValidationError", "Stack does not exist", expected_params=name)
    with cloudformation:
        stage.check_release_state()
    cloudformation.assert_no_pending_responses()


def test_both_census_gates_start_with_the_last_count(stage: _demand.DemandStage, stub: Any) -> None:
    ssm, cloudformation, stepfunctions = stub("ssm"), stub("cloudformation"), stub("stepfunctions")
    ssm.add_response(
        "get_parameter",
        {"Parameter": {"Name": "/fastcix/ops/running-channels", "Value": "1"}},
        {"Name": "/fastcix/ops/running-channels"},
    )
    arns = {
        "FastChannels-fastcix-Demand": "arn:aws:states:us-east-1:123456789012:stateMachine:fastcix-census-gate",
        "FastChannels-fastcix-Pds": "arn:aws:states:us-east-1:123456789012:stateMachine:fastcix-pds-census-gate",
    }
    detail = json.dumps({"detail": {"running": 1, "previous": 1, "correlation_id": "deploy-demand"}})
    for name, arn in arns.items():
        cloudformation.add_response(
            "describe_stacks", {"Stacks": [_stack(name, {"CensusGateArn": arn})]}, {"StackName": name}
        )
    for arn in arns.values():
        stepfunctions.add_response(
            "start_execution",
            {"executionArn": f"{arn}:x", "startDate": datetime(2026, 10, 29, tzinfo=UTC)},
            {"stateMachineArn": arn, "input": detail},
        )

    assert stage.start_census_gate() == f"{arns['FastChannels-fastcix-Demand']}:x"


def test_guidance_parameters_are_keyed_by_the_renamed_stack(stage: _demand.DemandStage) -> None:
    assert stage.guidance_parameters("fastcix-pbs", {"SpotInstanceWeight": "0", "EnableLogAnalytics": "false"}) == [
        "--parameters",
        "fastcix-pbs:SpotInstanceWeight=0",
        "--parameters",
        "fastcix-pbs:EnableLogAnalytics=false",
    ]


def test_a_parameter_not_in_the_registry_is_refused(stage: _demand.DemandStage) -> None:
    with pytest.raises(_demand.DemandError, match="not in GUIDANCE_PARAMETERS"):
        stage.guidance_parameters("fastcix-pbs", {"RequestsPerTargetThreshold": "100"})


def test_a_redeploy_takes_the_new_image_not_the_previous_parameter(
    stage: _demand.DemandStage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guidance's image is the default of its ContainerImage parameter: with previous parameter values reused, the
    4.4.0 → 3.43.0 rollback redeployed the template and kept 4.4.0 running."""
    monkeypatch.setattr(type(stage), "toolchain", lambda self: Path("/venv"))

    command = [str(arg) for arg in stage.guidance_command("prebid-server-deployment-on-aws", simulator=False)]

    assert "--no-previous-parameters" in command
    assert command[:3] == [str(_demand.CDK), "deploy", "prebid-server-deployment-on-aws"]


def test_the_lambda_simulator_is_reached_through_the_private_zone(stage: _demand.DemandStage) -> None:
    assert stage.simulator_endpoint("image") == {"SimulatorEndpoint": "http://sim.fastcix.fast.internal/"}


def test_smoke_passes_only_with_every_check() -> None:
    pod = {"status": 200, "content_type": "application/xml; charset=utf-8", "vast_4_2": True}
    results: dict[str, dict[str, Any]] = {
        **{f"{seconds}s": dict(pod) for seconds in (30, 60, 90, 120)},
        "no-secret": {"status": 403},
        "off-grid-31s": {"status": 400},
        "other-path": {"status": 403},
    }

    assert _demand.smoke_passed(results)
    for key, change in (
        ("60s", {"content_type": "application/json"}),
        ("90s", {"vast_4_2": False}),
        ("no-secret", {"status": 200}),
        ("off-grid-31s", {"status": 200}),
        ("other-path", {"status": 404}),
    ):
        broken = {k: dict(v) for k, v in results.items()}
        broken[key] |= change
        assert not _demand.smoke_passed(broken), key
    del results["120s"]
    assert not _demand.smoke_passed(results)


def test_empty_bucket_deletes_every_version_and_marker(stage: _demand.DemandStage, stub: Any) -> None:
    s3 = stub("s3")
    s3.add_response(
        "list_object_versions",
        {"Versions": [{"Key": "a", "VersionId": "1"}], "DeleteMarkers": [{"Key": "a", "VersionId": "2"}]},
        {"Bucket": "b"},
    )
    s3.add_response(
        "delete_objects",
        {},
        {
            "Bucket": "b",
            "Delete": {"Objects": [{"Key": "a", "VersionId": "1"}, {"Key": "a", "VersionId": "2"}], "Quiet": True},
        },
    )

    assert _demand.empty_bucket(stage.client("s3"), "b") == 2


def test_remove_retained_shortens_long_key_deletion_windows(stage: _demand.DemandStage, stub: Any) -> None:
    now = datetime.now(UTC)
    kms = stub("kms")
    kms.add_response(
        "describe_key",
        {"KeyMetadata": {"KeyId": "k30", "KeyState": "PendingDeletion", "DeletionDate": now + timedelta(days=30)}},
        {"KeyId": "k30"},
    )
    kms.add_response("cancel_key_deletion", {"KeyId": "k30"}, {"KeyId": "k30"})
    kms.add_response("schedule_key_deletion", {"KeyId": "k30"}, {"KeyId": "k30", "PendingWindowInDays": 7})
    kms.add_response(
        "describe_key",
        {"KeyMetadata": {"KeyId": "k7", "KeyState": "PendingDeletion", "DeletionDate": now + timedelta(days=7)}},
        {"KeyId": "k7"},
    )
    kms.add_response("describe_key", {"KeyMetadata": {"KeyId": "kon", "KeyState": "Enabled"}}, {"KeyId": "kon"})
    kms.add_response("schedule_key_deletion", {"KeyId": "kon"}, {"KeyId": "kon", "PendingWindowInDays": 7})
    logs = stub("logs")
    logs.add_client_error("delete_log_group", "ResourceNotFoundException", expected_params={"logGroupName": "gone"})
    ecs = stub("ecs")
    ecs.add_response("deregister_task_definition", {}, {"taskDefinition": "td:1"})
    ecs.add_response("delete_task_definitions", {}, {"taskDefinitions": ["td:1"]})
    s3 = stub("s3")
    s3.add_client_error("list_object_versions", "NoSuchBucket", expected_params={"Bucket": "gone-bucket"})

    stage.remove_retained(
        _demand.Retained(
            buckets=["gone-bucket"], log_groups=["gone"], keys=["k30", "k7", "kon"], task_definitions=["td:1"]
        )
    )


def test_parameters_are_read_and_deleted_by_path(stage: _demand.DemandStage, stub: Any) -> None:
    ssm = stub("ssm")
    page = {"Parameters": [{"Name": "/fastcix/demand/vpc-id", "Value": "vpc-1"}]}
    ssm.add_response("get_parameters_by_path", page, {"Path": "/fastcix/demand/"})
    ssm.add_response("get_parameters_by_path", page, {"Path": "/fastcix/demand/"})
    ssm.add_response(
        "delete_parameters", {"DeletedParameters": ["/fastcix/demand/vpc-id"]}, {"Names": ["/fastcix/demand/vpc-id"]}
    )

    assert stage.parameters() == {Parameter.VPC_ID.value: "vpc-1"}
    stage.delete_parameters()


def test_more_than_ten_parameters_are_deleted_ten_at_a_time(stage: _demand.DemandStage, stub: Any) -> None:
    ssm = stub("ssm")
    names = [f"/fastcix/demand/{parameter.value}" for parameter in Parameter]
    page = {"Parameters": [{"Name": name, "Value": "x"} for name in names]}
    ssm.add_response("get_parameters_by_path", page, {"Path": "/fastcix/demand/"})
    ssm.add_response("delete_parameters", {"DeletedParameters": names[:10]}, {"Names": names[:10]})
    ssm.add_response("delete_parameters", {"DeletedParameters": names[10:]}, {"Names": names[10:]})

    assert len(names) > 10
    stage.delete_parameters()


def test_retained_resources_are_read_page_by_page(stage: _demand.DemandStage, stub: Any) -> None:
    """DescribeStackResources stops at 100 resources; the guidance has about 350."""
    cloudformation = stub("cloudformation")
    row = {"LastUpdatedTimestamp": datetime(2026, 10, 29, tzinfo=UTC), "ResourceStatus": "CREATE_COMPLETE"}
    first = [
        {
            **row,
            "LogicalResourceId": f"B{n}",
            "PhysicalResourceId": f"fastcix-pbs-b{n}",
            "ResourceType": "AWS::S3::Bucket",
        }
        for n in range(100)
    ]
    second = [{**row, "LogicalResourceId": "K", "PhysicalResourceId": "k1", "ResourceType": "AWS::KMS::Key"}]
    cloudformation.add_response(
        "list_stack_resources", {"StackResourceSummaries": first, "NextToken": "t"}, {"StackName": "fastcix-pbs"}
    )
    cloudformation.add_response(
        "list_stack_resources", {"StackResourceSummaries": second}, {"StackName": "fastcix-pbs", "NextToken": "t"}
    )
    stack = {
        "StackName": "fastcix-pbs",
        "CreationTime": datetime(2026, 10, 29, tzinfo=UTC),
        "StackStatus": "CREATE_COMPLETE",
    }
    cloudformation.add_response("describe_stacks", {"Stacks": [stack]}, {"StackName": "fastcix-pbs"})
    cloudformation.add_client_error(
        "list_stack_resources", "ValidationError", expected_params={"StackName": "fastcix-pbs-sim"}
    )

    retained = stage.retained()

    assert len(retained.buckets) == 100
    assert retained.keys == ["k1"]


def test_deleted_network_resources_are_not_residue(stage: _demand.DemandStage, stub: Any) -> None:
    ec2 = stub("ec2")
    subnet = "arn:aws:ec2:us-east-1:123456789012:subnet/subnet-1"
    endpoint = "arn:aws:ec2:us-east-1:123456789012:vpc-endpoint/vpce-1"
    ec2.add_client_error("describe_subnets", "InvalidSubnetID.NotFound", expected_params={"SubnetIds": ["subnet-1"]})
    ec2.add_response(
        "describe_vpc_endpoints", {"VpcEndpoints": [{"VpcEndpointId": "vpce-1"}]}, {"VpcEndpointIds": ["vpce-1"]}
    )

    assert stage.gone(subnet, REGION)
    assert not stage.gone(endpoint, REGION)


def test_a_deleted_peering_connection_is_not_residue(stage: _demand.DemandStage, stub: Any) -> None:
    ec2 = stub("ec2")
    arn = "arn:aws:ec2:us-east-1:123456789012:vpc-peering-connection/pcx-1"
    for code in ("deleted", "active"):
        ec2.add_response(
            "describe_vpc_peering_connections",
            {"VpcPeeringConnections": [{"VpcPeeringConnectionId": "pcx-1", "Status": {"Code": code}}]},
            {"VpcPeeringConnectionIds": ["pcx-1"]},
        )

    assert stage.gone(arn, REGION)
    assert not stage.gone(arn, REGION)


def test_a_key_pending_deletion_is_not_residue(stage: _demand.DemandStage, stub: Any) -> None:
    kms = stub("kms")
    arn = "arn:aws:kms:us-east-1:123456789012:key/k1"
    kms.add_response("describe_key", {"KeyMetadata": {"KeyId": "k1", "KeyState": "PendingDeletion"}}, {"KeyId": arn})
    kms.add_response("describe_key", {"KeyMetadata": {"KeyId": "k1", "KeyState": "Enabled"}}, {"KeyId": arn})

    assert stage.gone(arn, REGION)
    assert not stage.gone(arn, REGION)


def test_a_deleted_nat_gateway_is_not_residue(stage: _demand.DemandStage, stub: Any) -> None:
    ec2 = stub("ec2")
    arn = "arn:aws:ec2:us-east-1:123456789012:natgateway/nat-1"
    ec2.add_response(
        "describe_nat_gateways",
        {"NatGateways": [{"NatGatewayId": "nat-1", "State": "deleted"}]},
        {"NatGatewayIds": ["nat-1"]},
    )

    assert stage.gone(arn, REGION)


def test_cli_refuses_a_stage_without_demand(capsys: pytest.CaptureFixture[str]) -> None:
    assert prebid_guidance.main(["inventory", "--prefix", "fastcix"]) == 1
    assert "modules.demand is off" in capsys.readouterr().err


def test_doctor_lists_the_tools(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        prebid_guidance, "iterate_tool_versions", lambda: iter([("git", "git 2"), ("cdk", "MISSING: x")])
    )

    assert prebid_guidance.main(["doctor"]) == 1
    assert "cdk              MISSING: x" in capsys.readouterr().out


STANDARD = Ladder.builtin("standard")


def demo() -> Any:
    encoded = {
        c.crid: (f"demo-{c.crid}-x", f"https://media.example.com/ads/standard/demo-{c.crid}-x/main.m3u8", 30030)
        for c in DEMO_CREATIVES
    }
    return demo_index(STANDARD.version, encoded, width=1920, height=1080)


def stack(name: str, outputs: dict[str, str]) -> dict[str, Any]:
    return {
        "StackName": name,
        "CreationTime": datetime(2026, 10, 29, tzinfo=UTC),
        "StackStatus": "CREATE_COMPLETE",
        "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outputs.items()],
    }


def test_the_demo_index_comes_from_the_media_bucket(stage: _demand.DemandStage, stub: Any) -> None:
    cloudformation, s3 = stub("cloudformation"), stub("s3")
    data = {"MediaBucketName": "fastcix-media"}
    media = {"DefaultLadderVersion": STANDARD.version, "ContentBaseUrl": "https://media.example.com"}
    cloudformation.add_response(
        "describe_stacks",
        {"Stacks": [stack("FastChannels-fastcix-Data", data)]},
        {"StackName": "FastChannels-fastcix-Data"},
    )
    cloudformation.add_response(
        "describe_stacks",
        {"Stacks": [stack("FastChannels-fastcix-Media", media)]},
        {"StackName": "FastChannels-fastcix-Media"},
    )
    s3.add_response(
        "get_object",
        {"Body": io.BytesIO(demo().model_dump_json().encode())},
        {"Bucket": "fastcix-media", "Key": demo_index_key(STANDARD.version)},
    )

    assert stage.demo_index() == demo()


def test_a_demand_only_stage_has_no_demo_index(stage: _demand.DemandStage, stub: Any) -> None:
    cloudformation = stub("cloudformation")
    cloudformation.add_client_error(
        "describe_stacks", "ValidationError", expected_params={"StackName": "FastChannels-fastcix-Data"}
    )

    assert stage.demo_index() is None


KIT_VAST: Final = "".join(
    [
        '<VAST xmlns="http://www.iab.com/VAST" version="4.2">',
        *(
            f'<Ad sequence="{n}"><InLine><Creatives><Creative id="h{n}-std-0a1b"><Linear>'
            "<Duration>00:00:30.030</Duration><MediaFiles>"
            f'<MediaFile width="1920" height="1080">https://media.example.com/ads/std/h{n}/main.m3u8</MediaFile>'
            "</MediaFiles></Linear></Creative></Creatives></InLine></Ad>"
            for n in (1, 2, 1)
        ),
        "</VAST>",
    ]
)
"""The kit's VAST pod (``ads/<ladder>/vast.xml``), which repeats its house ads."""


def media_stacks(cloudformation: Any) -> None:
    for name, outputs in (
        ("FastChannels-fastcix-Data", {"MediaBucketName": "fastcix-media"}),
        (
            "FastChannels-fastcix-Media",
            {"DefaultLadderVersion": STANDARD.version, "ContentBaseUrl": "https://m.example"},
        ),
    ):
        cloudformation.add_response("describe_stacks", {"Stacks": [stack(name, outputs)]}, {"StackName": name})


def test_the_modules_house_ads_are_the_kits(stage: _demand.DemandStage, stub: Any) -> None:
    cloudformation, s3 = stub("cloudformation"), stub("s3")
    media_stacks(cloudformation)
    s3.add_response(
        "get_object",
        {"Body": io.BytesIO(KIT_VAST.encode())},
        {"Bucket": "fastcix-media", "Key": vast_key(STANDARD.version)},
    )

    ads = stage.house_ads()

    assert [(ad.crid, ad.dur, ad.w) for ad in ads] == [("house1", 30, 1920), ("house2", 30, 1920)]
    assert "h1-std-0a1b" in ads[0].adm and "<Impression>" not in ads[0].adm


def test_without_a_kit_the_module_has_no_house_ads(stage: _demand.DemandStage, stub: Any) -> None:
    cloudformation, s3 = stub("cloudformation"), stub("s3")
    media_stacks(cloudformation)
    s3.add_client_error(
        "get_object", "NoSuchKey", expected_params={"Bucket": "fastcix-media", "Key": vast_key(STANDARD.version)}
    )

    assert stage.house_ads() == ()


def test_the_simulator_creatives_bill_on_the_cdn_beacon(stage: _demand.DemandStage) -> None:
    rendered = stage.simulator_creatives(demo(), "https://media.example.com/")

    assert rendered is not None
    creatives = json.loads(rendered)
    assert [c["crid"] for c in creatives] == [f"sim{n}" for n in range(1, 9)]
    assert creatives[0]["burl"].startswith("https://media.example.com/ads/b.gif?seat=amt&cr=sim1&p=${AUCTION_PRICE}")
    assert stage.simulator_creatives(None, "https://media.example.com") is None


def test_the_simulator_package_holds_the_handler_and_the_creatives(
    stage: _demand.DemandStage, stub: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = tmp_path / _demand.SIMULATOR_HANDLER
    handler.mkdir(parents=True)
    (handler / "handler.py").write_text("def lambda_handler(event, context): ...\n")
    (handler / "bid_response.json").write_text("{}")
    monkeypatch.setattr(type(stage), "checkout", property(lambda self: tmp_path))
    uploaded: list[dict[str, Any]] = []
    s3 = stub("s3")
    for _ in range(2):
        s3.add_response("put_object", {}, None)
    stage.client("s3").meta.events.register(
        "provide-client-params.s3.PutObject", lambda params, **_: uploaded.append(params)
    )

    key = stage.simulator_package('[{"crid": "sim1"}]')
    again = stage.simulator_package('[{"crid": "sim1"}]')

    assert key == again, "the key is the package's content hash"
    assert key.startswith("simulator/") and key.endswith(".zip")
    assert uploaded[0]["Bucket"] == "fastcix-cdk-assets-123456789012-us-east-1"
    body = uploaded[0]["Body"]
    with zipfile.ZipFile(io.BytesIO(body if isinstance(body, bytes) else body.getvalue())) as package:
        assert sorted(package.namelist()) == ["bid_response.json", "handler.py", "video_creatives.json"]


def test_the_network_names_each_subnets_zone_and_route_table(stage: _demand.DemandStage, stub: Any) -> None:
    ssm, ec2, elb, ecs = stub("ssm"), stub("ec2"), stub("elbv2"), stub("ecs")
    alb = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/fastcix-alb/0123456789abcdef"
    pbs = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/fastcix-pbs/0123456789abcdef"
    values = {
        "vpc-id": "vpc-1",
        "alb-arn": alb,
        "private-subnet-ids": "subnet-a,subnet-b",
        "task-security-group-id": "sg-1",
        "cluster-arn": "arn:aws:ecs:us-east-1:123456789012:cluster/c",
        "service-arn": "arn:aws:ecs:us-east-1:123456789012:service/c/s",
        "cloudfront-domain": "d1.cloudfront.net",
    }
    page = {"Parameters": [{"Name": f"/fastcix/demand/{k}", "Value": v} for k, v in values.items()]}
    ssm.add_response("get_parameters_by_path", page, {"Path": "/fastcix/demand/"})
    ec2.add_response(
        "describe_subnets",
        {
            "Subnets": [
                {"SubnetId": "subnet-a", "AvailabilityZone": "us-east-1a"},
                {"SubnetId": "subnet-b", "AvailabilityZone": "us-east-1b"},
            ]
        },
        {"SubnetIds": ["subnet-a", "subnet-b"]},
    )
    ec2.add_response(
        "describe_route_tables",
        {
            "RouteTables": [
                {"RouteTableId": "rtb-0a", "Associations": [{"SubnetId": "subnet-a"}]},
                {"RouteTableId": "rtb-0b", "Associations": [{"Main": True}]},
            ]
        },
        {"Filters": [{"Name": "vpc-id", "Values": ["vpc-1"]}]},
    )
    cache = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/fastcix-cache/0123456789abcdef"
    elb.add_response(
        "describe_target_groups",
        {
            "TargetGroups": [
                {"TargetGroupArn": cache, "TargetType": "lambda"},
                {"TargetGroupArn": pbs, "TargetType": "ip"},
            ]
        },
        {"LoadBalancerArn": alb},
    )
    task = "arn:aws:ecs:us-east-1:123456789012:task-definition/fastcix-pbs:3"
    ecs.add_response(
        "describe_services",
        {"services": [{"taskDefinition": task}]},
        {"cluster": values["cluster-arn"], "services": [values["service-arn"]]},
    )
    container = {
        "name": "Prebid-Container",
        "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "fastcix-pbs-PrebidContainerLogs"}},
    }
    ecs.add_response(
        "describe_task_definition",
        {"taskDefinition": {"containerDefinitions": [{"name": "sidecar"}, container]}},
        {"taskDefinition": task},
    )

    network = stage.network("simulator/0123456789abcdef.zip")

    assert [(s.id, s.az, s.route_table) for s in network.private_subnets] == [
        ("subnet-a", "us-east-1a", "rtb-0a"),
        ("subnet-b", "us-east-1b", "rtb-0b"),
    ]
    assert (network.vpc_id, network.task_security_group_id) == ("vpc-1", "sg-1")
    assert network.target_group_arn == pbs, "the Prebid Server targets, not the cache's Lambda"
    assert network.container_log_group == "fastcix-pbs-PrebidContainerLogs"


def test_a_task_without_one_log_group_or_a_missing_service_gives_none(stage: _demand.DemandStage, stub: Any) -> None:
    ecs = stub("ecs")

    def logging(name: str, group: str) -> dict[str, Any]:
        return {"name": name, "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": group}}}

    for containers, expected in (
        ([{"name": "c"}], None),
        ([logging("a", "g1"), logging("b", "g2")], None),
        ([logging("only", "g3")], "g3"),
    ):
        ecs.add_response("describe_services", {"services": [{"taskDefinition": "td"}]})
        ecs.add_response("describe_task_definition", {"taskDefinition": {"containerDefinitions": containers}})
        assert stage.container_log_group("cluster", "service") == expected
    ecs.add_response("describe_services", {"services": []})
    assert stage.container_log_group("cluster", "service") is None


def test_the_simulator_smoke_wants_one_demo_bid_per_slot(stage: _demand.DemandStage, stub: Any) -> None:
    def answer(crids: list[str]) -> dict[str, Any]:
        bids = [{"impid": f"s{n}", "crid": c, "adomain": ["x"], "price": 1, "dur": 30} for n, c in enumerate(crids, 1)]
        body = json.dumps({"id": "smoke", "seatbid": [{"bid": bids}]})
        return {"Payload": io.BytesIO(json.dumps({"statusCode": 200, "body": body}).encode())}

    lam = stub("lambda")
    lam.add_response("invoke", answer(["sim1", "sim2", "sim3", "sim4"]), None)
    lam.add_response("invoke", answer(["sim1", "sim2", "sim3", "banner_creative_1"]), None)
    lam.add_response("invoke", answer(["sim1", "sim2"]), None)

    good = stage.simulator_smoke(demo())
    assert good["passed"] and good["function"] == "fastcix-demand-simulator"
    assert [b["crid"] for b in good["bids"]] == ["sim1", "sim2", "sim3", "sim4"]
    assert not stage.simulator_smoke(demo())["passed"], "not a demo creative"
    assert not stage.simulator_smoke(demo())["passed"], "a slot without a bid"


def test_the_smoke_counts_the_simulator_and_the_creatives() -> None:
    results: dict[str, Any] = {
        **{f"{s}s": {"status": 200, "content_type": "application/xml", "vast_4_2": True} for s in (30, 60, 90, 120)},
        "no-secret": {"status": 403},
        "off-grid-31s": {"status": 400},
        "other-path": {"status": 403},
        "simulator": {"passed": True},
        "demo-creatives": {"passed": True},
    }

    assert _demand.smoke_passed(results)
    for check in ("simulator", "demo-creatives"):
        assert not _demand.smoke_passed({**results, check: {"passed": False}}), check
