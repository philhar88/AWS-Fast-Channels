"""Deploy and destroy a stage's demand module: the repository's DemandEdge and DemandBuild stacks, the guidance's
dedicated CDK bootstrap, the Prebid Server image, the guidance's stacks and the SSM contract (docs/demand.md).

Used by ``scripts/prebid_guidance.py`` (``doctor``, ``bootstrap``, ``build-image``, ``deploy``, ``outputs``,
``upload-config``, ``smoke``, ``destroy``, ``inventory``). Deploy order (DESIGN §4.3): DemandEdge and DemandBuild →
bootstrap → image → guidance (simulator stack first in ``guidance_stack`` mode) → SSM parameters → Demand (and Pds
with ``modules.pds.enabled``) → the census gates once. Destroy runs it backwards and also removes what the guidance's
templates retain: its S3 buckets (every version), log groups, inactive task definitions; its KMS keys are scheduled for
deletion with the 7-day minimum, the only residue.
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qs, urlsplit

import httpx
import stage_args
from botocore.exceptions import ClientError

from fast_channels.core import demand as names
from fast_channels.core.demand import NETWORK_CONTEXT, SIMULATOR_CODE_PREFIX, DemandNetwork, Parameter, Subnet
from fast_channels.core.demo_demand import DemoIndex, DemoRole, demo_index_key, simulator_creatives
from fast_channels.core.ladder import Ladder
from fast_channels.core.manifests import expected_signature, ladder_signature, parse_multivariant
from fast_channels.core.monetization import names as release_names
from fast_channels.core.pbs_config import POD_SECONDS, render, simulator_host
from fast_channels.core.pds import names as pds_names

if TYPE_CHECKING:
    import boto3

    from fast_channels.infra.config import FastChannelsConfig

REPO: Final = Path(__file__).resolve().parents[1]
VENDOR: Final = REPO / "vendor" / "prebid-guidance"
BUILD: Final = REPO / "build" / "prebid-guidance"
VENV: Final = BUILD / ".venv"
CDK: Final = REPO / "node_modules" / ".bin" / "cdk"
GUIDANCE_PYTHON: Final = "3.11"
"""The guidance's Lambda runtime; its local bundling installs packages with the deploying interpreter's pip."""
GUIDANCE_STACK_ID: Final = "prebid-server-deployment-on-aws"
SIMULATOR_STACK_ID: Final = "BiddingServerSimulator"
CONFIG_ROOT: Final = Path("deployment/ecr/prebid-server")
SIMULATOR_HANDLER: Final = Path("source/loadtest/bidder_simulator/lambdas/loadtest_bidder")
"""The guidance's bidder simulator handler (overlay O5 reads ``video_creatives.json`` beside it)."""
SIMULATOR_SLOTS: Final = 4
GUIDANCE_PARAMETERS: Final = frozenset(
    {
        "SpotInstanceWeight",
        "ECSTaskMinCapacity",
        "ECSTaskMaxCapacity",
        "EnableLogAnalytics",
        "ExtraWafRuleGroupArn",
        "SimulatorVpcId",
        "SimulatorAlbSgId",
        "SimulatorRouteTableId1",
        "SimulatorRouteTableId2",
        "SimulatorEndpoint",
        "EnableRtbFabric",
    }
)
"""The guidance's CloudFormation parameters a deploy sets. Its stacks deploy with --no-previous-parameters, so each one
the mode uses is passed on every deploy (the Simulator* ones only for the simulator modes that need them)."""
GUIDANCE_DEFAULTS: Final = {
    "ContainerImage": "the image: its default is the image the deploy synthesizes with (OVERRIDE_ECR_REGISTRY)",
    "ContainerImageUri": "declared by the guidance v1.4.0 and read nowhere",
    "InstallCloudFrontAndWAF": "Yes: MediaTailor reaches Prebid Server through the guidance's CloudFront and WAF",
    "SSLCertificateARN": "empty: the ALB is reached only through CloudFront",
    "RequestsPerTargetThreshold": "the guidance's request-count target tracking",
    "EnableRtbRequesterGateway": "false: no RTB Fabric",
    "bidResponseDelayPercentage": "0: the simulator answers without artificial delays",
    "bidResponseTimeoutPercentage": "0: the simulator never times out on purpose",
    "bidResponseDelayProbability": "0",
    "bidResponseTimeoutProbability": "0",
}
"""The guidance's other parameters, left at their defaults on purpose."""
GUIDANCE_JDK: Final = "21"
"""The Corretto major version the guidance's Dockerfile installs by default (overlay O6's ``JDK`` argument)."""
KMS_PENDING_DAYS: Final = 7
EC2_DESCRIBE: Final = {
    "subnet": ("describe_subnets", "SubnetIds"),
    "vpc": ("describe_vpcs", "VpcIds"),
    "vpc-endpoint": ("describe_vpc_endpoints", "VpcEndpointIds"),
    "security-group": ("describe_security_groups", "GroupIds"),
    "route-table": ("describe_route_tables", "RouteTableIds"),
    "internet-gateway": ("describe_internet_gateways", "InternetGatewayIds"),
    "elastic-ip": ("describe_addresses", "AllocationIds"),
    "network-acl": ("describe_network_acls", "NetworkAclIds"),
}
"""EC2 resource types the tagging index keeps listing for a while after they are deleted: the describe call (which
fails with ``*.NotFound``, or returns nothing) that tells."""
TAGS: Final = {"fast:owner": "iac", names.MODULE_TAG: names.MODULE}
"""Tags of everything the demand module deploys, besides ``fast:app``."""
_LOCK_GROUPS: Final = {"main", "local", "dev"}
"""poetry.lock groups the guidance's ``poetry install`` installs; its CDK helpers import cdk-nag (dev)."""


class DemandError(RuntimeError):
    """A deploy, destroy or check step failed."""


def log(message: str) -> None:
    print(f"[demand] {message}", flush=True)


def run(
    argv: Sequence[str | Path], *, cwd: Path = REPO, env: Mapping[str, str] | None = None, stream: bool = False
) -> str:
    """Run a tool and return its standard output; with ``stream``, print its output as it comes (long deploys).
    Raise DemandError with the tail of its output on failure."""
    command = [str(arg) for arg in argv]
    if not stream:
        result = subprocess.run(  # noqa: S603 - argv built here, no shell
            command, cwd=cwd, env=dict(env) if env else None, capture_output=True, text=True, check=False
        )
        output, code = result.stdout + result.stderr, result.returncode
    else:
        lines: list[str] = []
        with subprocess.Popen(  # noqa: S603 - argv built here, no shell
            command,
            cwd=cwd,
            env=dict(env) if env else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as process:
            assert process.stdout is not None
            for line in process.stdout:
                print(f"  {line}", end="", flush=True)
                lines.append(line)
        output, code = "".join(lines), process.returncode
        result = subprocess.CompletedProcess(command, code, output, "")
    if code != 0:
        tail = "\n".join(output.splitlines()[-60:])
        raise DemandError(f"{Path(command[0]).name} {' '.join(command[1:4])} … failed:\n{tail}")
    return str(result.stdout)


def lock_requirements(lock: Path) -> tuple[list[str], list[str]]:
    """Hashed requirement lines for the guidance's ``poetry.lock`` (main, local and dev groups, markers kept), and
    the paths of its local packages."""
    requirements, local = [], []
    for package in tomllib.loads(lock.read_text())["package"]:
        groups = set(package.get("groups", []))
        if not groups & _LOCK_GROUPS:
            continue
        source = package.get("source", {})
        if source.get("type") == "directory":
            local.append(source["url"])
            continue
        markers = package.get("markers")
        if isinstance(markers, dict):
            markers = " or ".join(f"({markers[g]})" for g in sorted(_LOCK_GROUPS & set(markers))) or None
        hashes = " ".join(f"--hash={entry['hash']}" for entry in package["files"])
        requirements.append(f"{package['name']}=={package['version']}{f' ; {markers}' if markers else ''} {hashes}")
    return requirements, local


def context_digest(root: Path) -> str:
    """Content hash of a build context (paths and bytes), the image tag: equal contexts build equal images."""
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def zip_directory(root: Path) -> bytes:
    """``root`` as a zip with fixed timestamps and modes (deterministic)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(str(path.relative_to(root)), date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = (0o755 if os.access(path, os.X_OK) else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return buffer.getvalue()


PREBID_CONTAINER: Final = "Prebid-Container"
"""The guidance's Prebid Server container, whose log the fallback alarms read."""


def console_bucket(url: str) -> str:
    """The bucket of an S3 console URL output (``...?region=<r>&bucket=<name>``)."""
    return parse_qs(urlsplit(url).query)["bucket"][0]


def empty_bucket(s3: Any, bucket: str) -> int:
    """Delete every object version and delete marker of ``bucket``; returns how many."""
    deleted = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
        keys = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
        ]
        for start in range(0, len(keys), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys[start : start + 1000], "Quiet": True})
        deleted += len(keys)
    return deleted


def smoke_passed(results: Mapping[str, Mapping[str, Any]]) -> bool:
    """Every grid duration answered 200 with a VAST 4.2 document as XML; the POST without the origin secret and the
    request to another path were blocked at the edge (403); the off-grid duration was refused (400)."""
    pods = [result for key, result in results.items() if key.removesuffix("s").isdigit()]
    return (
        len(pods) == len(POD_SECONDS)
        and all(
            r["status"] == 200 and str(r["content_type"]).startswith("application/xml") and r["vast_4_2"] for r in pods
        )
        and results["no-secret"]["status"] == 403
        and results["off-grid-31s"]["status"] == 400
        and results["other-path"]["status"] == 403
        and all(results[check]["passed"] for check in ("simulator", "demo-creatives") if check in results)
    )


@dataclass
class Retained:
    """What the guidance's templates leave behind when their stacks are deleted."""

    buckets: list[str] = field(default_factory=list)
    log_groups: list[str] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    task_definitions: list[str] = field(default_factory=list)

    def merge(self, other: Retained) -> Retained:
        return Retained(
            sorted({*self.buckets, *other.buckets}),
            sorted({*self.log_groups, *other.log_groups}),
            sorted({*self.keys, *other.keys}),
            sorted({*self.task_definitions, *other.task_definitions}),
        )


def retained_resources(resources: Sequence[Mapping[str, Any]], cluster: str | None) -> Retained:
    """The resources of a guidance stack that outlive it (``list_stack_resources`` rows): buckets, log groups,
    keys and task definitions, plus the log groups Lambda and Container Insights create on their own."""
    found = Retained()
    for resource in resources:
        physical = resource.get("PhysicalResourceId")
        if not physical:
            continue
        match resource["ResourceType"]:
            case "AWS::S3::Bucket":
                found.buckets.append(physical)
            case "AWS::Logs::LogGroup":
                found.log_groups.append(physical)
            case "AWS::Lambda::Function":
                found.log_groups.append(f"/aws/lambda/{physical}")
            case "AWS::KMS::Key":
                found.keys.append(physical)
            case "AWS::ECS::TaskDefinition":
                found.task_definitions.append(physical)
    if cluster:
        found.log_groups.append(f"/aws/ecs/containerinsights/{cluster}/performance")
    return found


@dataclass
class DemandStage:
    """One stage's demand module in one account and region."""

    config: FastChannelsConfig
    stage: str | None
    """The stage file (``stages/<stage>.json``) the repository's stacks are synthesized with, if any."""
    session: boto3.Session
    modules: list[str] | None = None
    """Only these modules of the stage on (``--modules``), or all the stage names."""
    _clients: dict[tuple[str, str], Any] = field(default_factory=dict, init=False, repr=False)

    @property
    def prefix(self) -> str:
        return self.config.prefix

    @cached_property
    def account(self) -> str:
        account: str = self.session.client("sts").get_caller_identity()["Account"]
        return account

    @property
    def region(self) -> str:
        if not (region := self.session.region_name):
            raise DemandError("no AWS region configured: set AWS_REGION")
        return region

    @property
    def checkout(self) -> Path:
        """The stage's own guidance checkout (rendered configuration included)."""
        return BUILD / self.prefix

    def client(self, service: str, region: str | None = None) -> Any:
        """One client per service and region (tests stub them)."""
        key = (service, region or self.region)
        if key not in self._clients:
            self._clients[key] = self.session.client(service, region_name=key[1])  # type: ignore[call-overload]
        return self._clients[key]

    # ----- the repository's stacks -----------------------------------------------------------------------------

    def repository_context(self) -> list[str]:
        """CDK CLI context arguments for the repository's app: the stage file under this prefix."""
        if self.stage:
            return stage_args.arguments(stage_args.resolve(self.stage, self.prefix, self.modules))
        return ["-c", f"fast-channels.prefix={self.prefix}"]

    @property
    def stage_id(self) -> str:
        return "FastChannels" if self.prefix == "fast" else f"FastChannels-{self.prefix}"

    def repository_stacks(self) -> list[str]:
        return [f"{self.stage_id}/DemandEdge", f"{self.stage_id}/DemandBuild"]

    def deploy_repository_stacks(self) -> None:
        log(f"deploying {', '.join(self.repository_stacks())}")
        run(
            [
                CDK,
                "deploy",
                *self.repository_stacks(),
                "--exclusively",
                "--require-approval",
                "never",
                *self.repository_context(),
            ],
            env=self.cdk_environment(),
            stream=True,
        )

    def destroy_repository_stacks(self) -> None:
        log(f"destroying {', '.join(self.repository_stacks())}")
        run(
            [CDK, "destroy", *self.repository_stacks(), "--exclusively", "--force", *self.repository_context()],
            env=self.cdk_environment(),
            stream=True,
        )

    def stack_outputs(self, name: str, region: str | None = None) -> dict[str, str]:
        try:
            [stack] = self.client("cloudformation", region).describe_stacks(StackName=name)["Stacks"]
        except ClientError as error:
            raise DemandError(f"stack {name} does not exist: deploy it first ({error})") from None
        return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}

    @property
    def edge_stack(self) -> str:
        return f"{self.stage_id}-DemandEdge" + ("" if self.region == "us-east-1" else f"-{self.region}")

    @property
    def build_stack(self) -> str:
        return f"{self.stage_id}-DemandBuild"

    def cdk_environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return {
            **os.environ,
            "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1",
            "AWS_REGION": self.region,
            **(extra or {}),
        }

    # ----- the guidance's toolchain and bootstrap ---------------------------------------------------------------

    def toolchain(self) -> Path:
        """The guidance's Python environment (its pinned ``poetry.lock``, hashes required), once per lock."""
        lock = self.checkout / "source" / "poetry.lock"
        stamp = VENV / ".fast-lock"
        digest = hashlib.sha256(lock.read_bytes()).hexdigest()
        if stamp.is_file() and stamp.read_text() == digest:
            return VENV
        log(f"creating the guidance's Python {GUIDANCE_PYTHON} environment from its poetry.lock")
        shutil.rmtree(VENV, ignore_errors=True)
        run(["uv", "venv", "--quiet", "--seed", "--python", GUIDANCE_PYTHON, VENV])
        requirements, local = lock_requirements(lock)
        requirements_file = BUILD / "guidance-requirements.txt"
        requirements_file.write_text("\n".join(requirements) + "\n")
        environment = {**os.environ, "VIRTUAL_ENV": str(VENV)}
        run(["uv", "pip", "install", "--quiet", "--require-hashes", "-r", requirements_file], env=environment)
        helpers = [self.checkout / "source" / path for path in local if path.startswith("cdk_solution_helper_py")]
        run(["uv", "pip", "install", "--quiet", "--no-deps", *helpers], env=environment)
        stamp.write_text(digest)
        return VENV

    def bootstrap(self) -> None:
        """The guidance's dedicated CDK bootstrap: qualifier = prefix, prefixed bucket and repository, our
        execution policy, the AWS managed S3 key (no key of its own to wait out on teardown)."""
        policy = self.stack_outputs(self.build_stack)["GuidanceExecutionPolicyArn"]
        template = run([CDK, "bootstrap", "--show-template"], env=self.cdk_environment())
        parameters = {
            "Qualifier": names.bootstrap_qualifier(self.prefix),
            "FileAssetsBucketName": names.assets_bucket(self.prefix, self.account, self.region),
            "ContainerAssetsRepositoryName": names.images_repository(self.prefix, self.account, self.region),
            "CloudFormationExecutionPolicies": policy,
            "FileAssetsBucketKmsKeyId": "AWS_MANAGED_KEY",
            "PublicAccessBlockConfiguration": "true",
            "TrustedAccounts": "",
            "TrustedAccountsForLookup": "",
        }
        self.apply_stack(names.toolkit_stack(self.prefix), template, parameters)

    def apply_stack(self, name: str, template: str, parameters: Mapping[str, str]) -> None:
        """Create or update a stack from a template body and wait."""
        cloudformation = self.client("cloudformation")
        request = {
            "StackName": name,
            "TemplateBody": template,
            "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()],
            "Capabilities": ["CAPABILITY_NAMED_IAM"],
            "Tags": [{"Key": k, "Value": v} for k, v in self.tags().items()],
        }
        try:
            cloudformation.describe_stacks(StackName=name)
            exists = True
        except ClientError:
            exists = False
        log(f"{'updating' if exists else 'creating'} {name}")
        try:
            if exists:
                cloudformation.update_stack(**request)
            else:
                cloudformation.create_stack(**request, OnFailure="DELETE")
        except ClientError as error:
            if "No updates are to be performed" in str(error):
                return
            raise
        waiter = cloudformation.get_waiter("stack_update_complete" if exists else "stack_create_complete")
        waiter.wait(StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 180})

    def tags(self) -> dict[str, str]:
        return {"fast:app": self.prefix, **TAGS}

    # ----- the image ---------------------------------------------------------------------------------------------

    def build_context(self, out: Path, release: tuple[str, Any] | None = None) -> Path:
        """The guidance's Docker build context with its ``amt-bidder`` sources (overlays and module included), and
        our build script, under ``out``; for a release the guidance does not build (``release``: its version and
        ``PIN.toml [pbs_java.builds]`` entry), a ``docker-build-config.json`` for it."""
        shutil.rmtree(out, ignore_errors=True)
        shutil.copytree(self.checkout / CONFIG_ROOT, out / "context")
        shutil.rmtree(out / "context" / "amt-bidder")
        shutil.copytree(self.checkout / "source" / "loadtest" / "amt-bidder", out / "context" / "amt-bidder")
        shutil.copy2(VENDOR / "build-image.sh", out / "build-image.sh")
        if release is not None:
            version, spec = release
            config_file = out / "context" / "docker-build-config.json"
            config = json.loads(config_file.read_text())
            config = {
                "GIT_TAG_VERSION": version,
                "GIT_REPO_URL": config["GIT_REPO_URL"],
                "MVN_CLI_OPTIONS": spec.mvn_options,
            }
            config_file.write_text(json.dumps(config, indent=2) + "\n")
        return out

    def build_image(self, pin: Any) -> str:
        """Build the Prebid Server image on the stage's x86 CodeBuild project (skipped when the repository already
        has the context's tag); returns the image URI."""
        outputs = self.stack_outputs(self.build_stack)
        repository_uri, project = outputs["ImageRepositoryUri"], outputs["ImageBuildProject"]
        config = json.loads((self.checkout / CONFIG_ROOT / "docker-build-config.json").read_text())
        version, jdk = config["GIT_TAG_VERSION"], GUIDANCE_JDK
        release: tuple[str, Any] | None = None
        if (wanted := self.config.modules.demand.pbs_version.value) != version:
            if (spec := pin.pbs_java_builds.get(wanted)) is None:
                raise DemandError(f"PIN.toml [pbs_java.builds] has no build of Prebid Server Java {wanted}")
            version, jdk, release = wanted, spec.jdk, (wanted, spec)
        if version not in pin.pbs_java_commits:
            raise DemandError(f"PIN.toml [pbs_java.commits] has no commit for Prebid Server Java {version}")
        context = self.build_context(BUILD / f"{self.prefix}-image", release)
        tag = f"{version}-{context_digest(context)}"
        image = f"{repository_uri}:{tag}"
        ecr = self.client("ecr")
        try:
            ecr.describe_images(repositoryName=names.image_repository(self.prefix), imageIds=[{"imageTag": tag}])
            log(f"image {image} exists")
            return image
        except ClientError as error:
            if error.response["Error"]["Code"] != "ImageNotFoundException":
                raise
        bucket = names.assets_bucket(self.prefix, self.account, self.region)
        key = f"{names.IMAGE_SOURCE_PREFIX}context-{tag}.zip"
        self.client("s3").put_object(Bucket=bucket, Key=key, Body=zip_directory(context))
        codebuild = self.client("codebuild")
        build = codebuild.start_build(
            projectName=project,
            sourceTypeOverride="S3",
            sourceLocationOverride=f"{bucket}/{key}",
            environmentVariablesOverride=[
                {"name": "IMAGE_TAG", "value": tag, "type": "PLAINTEXT"},
                {"name": "PBS_JAVA_REPOSITORY", "value": config["GIT_REPO_URL"], "type": "PLAINTEXT"},
                {"name": "PBS_JAVA_TAG", "value": version, "type": "PLAINTEXT"},
                {"name": "PBS_JAVA_COMMIT", "value": pin.pbs_java_commits[version], "type": "PLAINTEXT"},
                {"name": "PBS_JDK", "value": jdk, "type": "PLAINTEXT"},
            ],
        )["build"]
        log(f"building {image} ({build['id']})")
        while True:
            time.sleep(20)
            [state] = codebuild.batch_get_builds(ids=[build["id"]])["builds"]
            if state["buildStatus"] != "IN_PROGRESS":
                break
        if state["buildStatus"] != "SUCCEEDED":
            raise DemandError(
                f"image build {build['id']} {state['buildStatus']}: {state.get('logs', {}).get('deepLink')}"
            )
        log(f"built {image} in {round((state['endTime'] - state['startTime']).total_seconds())} s")
        return image

    # ----- the guidance --------------------------------------------------------------------------------------------

    def guidance_parameters(self, stack: str, values: Mapping[str, str]) -> list[str]:
        if unknown := sorted(set(values) - GUIDANCE_PARAMETERS):
            raise DemandError(f"not in GUIDANCE_PARAMETERS (a redeploy would reset them): {unknown}")
        """``--parameters <stack name>:<key>=<value>``: CDK keys stack parameters by the stack's name."""
        return [arg for key, value in values.items() for arg in ("--parameters", f"{stack}:{key}={value}")]

    def guidance_command(self, *stacks: str, simulator: bool, extra: Sequence[str] = ()) -> list[str | Path]:
        venv = self.toolchain()
        return [
            CDK,
            "deploy",
            *stacks,
            "--app",
            f"{venv / 'bin' / 'python'} app.py",
            "--require-approval",
            "never",
            # The guidance takes its image as the default of a CloudFormation parameter (ContainerImage); cdk deploy
            # would keep a parameter's previous value, so a redeploy with a new image left the old one running. Every
            # parameter we set is passed on each deploy (deploy_guidance), the others keep their defaults.
            "--no-previous-parameters",
            "-c",
            f"stackPrefix={self.prefix}",
            "-c",
            f"bootstrapQualifier={names.bootstrap_qualifier(self.prefix)}",
            "-c",
            f"fileAssetsBucketName={names.assets_bucket(self.prefix, self.account, self.region)}",
            "-c",
            f"imageAssetsRepositoryName={names.images_repository(self.prefix, self.account, self.region)}",
            "-c",
            f"deployBiddingSimulator={'true' if simulator else 'false'}",
            *(arg for key, value in self.tags().items() for arg in ("--tags", f"{key}={value}")),
            *extra,
        ]

    def guidance_environment(self, image: str) -> dict[str, str]:
        venv = self.toolchain()
        return self.cdk_environment(
            {"OVERRIDE_ECR_REGISTRY": image, "PATH": f"{venv / 'bin'}:{os.environ['PATH']}", "VIRTUAL_ENV": str(venv)}
        )

    def render_into_checkout(self, content_base_url: str, demo: DemoIndex | None = None) -> None:
        """The stage's Prebid Server configuration into the checkout, which the guidance uploads (overlay O3), and
        the demo creatives beside the simulator's handler (overlay O5), which its stack packages."""
        from prebid_guidance import pbs_settings  # noqa: PLC0415 - the CLI module imports this one

        for rel, content in render(pbs_settings(self.config, content_base_url)).items():
            path = self.checkout / CONFIG_ROOT / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        creatives = self.checkout / SIMULATOR_HANDLER / "video_creatives.json"
        if (rendered := self.simulator_creatives(demo, content_base_url)) is not None:
            creatives.write_text(rendered)
        else:
            creatives.unlink(missing_ok=True)
        site = self.checkout / "source" / "loadtest" / "demo" / "dist"
        if not site.is_dir():  # the simulator stack serves a demo page we never use; it must exist to synthesize
            site.mkdir(parents=True)
            (site / "index.html").write_text("<!doctype html><title>FAST Channels demand</title>\n")

    def simulator_endpoint(self, image: str) -> dict[str, str]:
        """The PBS stack's simulator parameters for the configured mode."""
        demand = self.config.modules.demand
        if demand.mode != "demo":
            return {}
        if demand.simulator == "lambda":
            # The simulator Lambda behind an internal load balancer in the PBS VPC, named in the stage's private
            # zone (the Demand stack). Prebid Server resolves it per request, so it may come up after the guidance.
            return {"SimulatorEndpoint": f"http://{simulator_host(self.prefix)}/"}
        sim = names.simulator_stack(self.prefix)
        run(
            self.guidance_command(
                SIMULATOR_STACK_ID, simulator=True, extra=self.guidance_parameters(sim, {"EnableRtbFabric": "false"})
            ),
            cwd=self.checkout / "source" / "infrastructure",
            env=self.guidance_environment(image),
            stream=True,
        )
        outputs = self.stack_outputs(sim)
        return {
            "SimulatorVpcId": outputs["BidderSimulatorVpcId"],
            "SimulatorAlbSgId": outputs["BidderSimulatorAlbSecurityGroupId"],
            "SimulatorRouteTableId1": outputs["BidderSimulatorRouteTableId1"],
            "SimulatorRouteTableId2": outputs["BidderSimulatorRouteTableId2"],
            "SimulatorEndpoint": outputs["BidderSimulatorAlbEndpoint"],
        }

    def deploy_guidance(self, image: str) -> None:
        demand = self.config.modules.demand
        rule_group = self.stack_outputs(self.edge_stack, "us-east-1")["OriginRuleGroupArn"]
        simulator = demand.mode == "demo" and demand.simulator == "guidance_stack"
        values = {
            "SpotInstanceWeight": "0",
            "ECSTaskMinCapacity": str(demand.tasks.min),
            "ECSTaskMaxCapacity": str(demand.tasks.max),
            # Its reporter logs every request header, the ticket and the origin secret too (tests/pbs)
            "EnableLogAnalytics": "false",
            "ExtraWafRuleGroupArn": rule_group,
            **self.simulator_endpoint(image),
        }
        stack = names.guidance_stack(self.prefix)
        log(f"deploying {stack} with {image}")
        run(
            self.guidance_command(
                GUIDANCE_STACK_ID, simulator=simulator, extra=self.guidance_parameters(stack, values)
            ),
            cwd=self.checkout / "source" / "infrastructure",
            env=self.guidance_environment(image),
            stream=True,
        )

    # ----- beside the guidance: the demo creatives and the Demand stack ------------------------------------------

    @property
    def demand_stack(self) -> str:
        return f"{self.stage_id}-Demand"

    @property
    def pds_stack(self) -> str:
        return f"{self.stage_id}-Pds"

    @property
    def release_stack(self) -> str:
        return release_names.stack_name(self.stage_id)

    def beside_stacks(self) -> list[str]:
        """The repository's stacks that need the guidance's network, in deploy order: Demand, then Pds and Release
        if their modules are on (the release bundle names the guidance's CloudFront domain)."""
        modules = self.config.modules
        return (
            [f"{self.stage_id}/Demand"]
            + ([f"{self.stage_id}/Pds"] if modules.pds.enabled else [])
            + ([f"{self.stage_id}/Release"] if modules.release.enabled else [])
        )

    def media_outputs(self) -> dict[str, str] | None:
        """The Data and Media stacks' outputs, or None when the stage has no Media stack (a demand-only stage)."""
        try:
            return {**self.stack_outputs(f"{self.stage_id}-Data"), **self.stack_outputs(f"{self.stage_id}-Media")}
        except DemandError:
            return None

    def demo_index(self) -> DemoIndex | None:
        """The demo creatives of the stage's default ladder (``ads/<ladder>/demo/creatives.json``), or None: not in
        demo mode, no Media stack, or a kit without them. Without them the simulator bids the guidance's creatives."""
        if self.config.modules.demand.mode != "demo" or (outputs := self.media_outputs()) is None:
            return None
        key = demo_index_key(outputs["DefaultLadderVersion"])
        try:
            body = self.client("s3").get_object(Bucket=outputs["MediaBucketName"], Key=key)["Body"].read()
        except ClientError:
            log(f"no {key} in the media bucket: the simulator bids the guidance's own creatives")
            return None
        return DemoIndex.model_validate_json(body)

    @staticmethod
    def simulator_creatives(demo: DemoIndex | None, content_base_url: str) -> str | None:
        """The simulator's ``video_creatives.json`` for overlay O5, or None without demo creatives."""
        if demo is None:
            return None
        return json.dumps(simulator_creatives(demo, beacon_url=f"{content_base_url.rstrip('/')}/ads/b.gif"), indent=2)

    def simulator_package(self, creatives: str | None) -> str:
        """The ``lambda`` simulator's code (the guidance's handler with overlay O5, and the creatives) in the
        guidance's assets bucket, under its content hash; returns the key."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("handler.py", "bid_response.json"):
                shutil.copy(self.checkout / SIMULATOR_HANDLER / name, root / name)
            if creatives is not None:
                (root / "video_creatives.json").write_text(creatives)
            package = zip_directory(root)
        key = f"{SIMULATOR_CODE_PREFIX}{hashlib.sha256(package).hexdigest()[:16]}.zip"
        bucket = names.assets_bucket(self.prefix, self.account, self.region)
        self.client("s3").put_object(Bucket=bucket, Key=key, Body=package)
        log(f"simulator code s3://{bucket}/{key}")
        return key

    def network(self, simulator_code_key: str | None = None) -> DemandNetwork:
        """The guidance's network from the SSM contract and EC2: private subnets with their zones and route tables."""
        parameters = self.parameters()
        ids = parameters[Parameter.PRIVATE_SUBNET_IDS].split(",")
        ec2 = self.client("ec2")
        zones = {s["SubnetId"]: s["AvailabilityZone"] for s in ec2.describe_subnets(SubnetIds=ids)["Subnets"]}
        tables: dict[str, str] = {}
        main = ""
        for table in ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [parameters[Parameter.VPC_ID]]}])[
            "RouteTables"
        ]:
            for association in table.get("Associations", []):
                if association.get("Main"):
                    main = table["RouteTableId"]
                elif subnet := association.get("SubnetId"):
                    tables[subnet] = table["RouteTableId"]
        groups = self.client("elbv2").describe_target_groups(LoadBalancerArn=parameters[Parameter.ALB_ARN])
        [target_group] = [g["TargetGroupArn"] for g in groups["TargetGroups"] if g["TargetType"] == "ip"]
        return DemandNetwork(
            container_log_group=self.container_log_group(
                parameters[Parameter.CLUSTER_ARN], parameters[Parameter.SERVICE_ARN]
            ),
            vpc_id=parameters[Parameter.VPC_ID],
            private_subnets=tuple(Subnet(id=i, az=zones[i], route_table=tables.get(i, main)) for i in ids),
            task_security_group_id=parameters[Parameter.TASK_SECURITY_GROUP],
            cluster_arn=parameters[Parameter.CLUSTER_ARN],
            service_arn=parameters[Parameter.SERVICE_ARN],
            cloudfront_domain=parameters[Parameter.CLOUDFRONT_DOMAIN],
            target_group_arn=target_group,
            simulator_code_key=simulator_code_key,
        )

    def container_log_group(self, cluster: str, service: str) -> str | None:
        """The log group of Prebid Server's container (``Prebid-Container`` in the guidance's task definition, else its
        only container that logs to CloudWatch), or None without one."""
        ecs = self.client("ecs")
        services = ecs.describe_services(cluster=cluster, services=[service])["services"]
        if not services:
            log(f"no service {service}: the fallback alarms are left out")
            return None
        definition = ecs.describe_task_definition(taskDefinition=services[0]["taskDefinition"])["taskDefinition"]
        groups = {
            container["name"]: container["logConfiguration"]["options"]["awslogs-group"]
            for container in definition["containerDefinitions"]
            if container.get("logConfiguration", {}).get("logDriver") == "awslogs"
        }
        if PREBID_CONTAINER in groups:
            return str(groups[PREBID_CONTAINER])
        return str(next(iter(groups.values()))) if len(groups) == 1 else None

    def deploy_demand_stack(self, demo: DemoIndex | None, content_base_url: str) -> None:
        """``FastChannels-<prefix>-Demand`` (the private zone, and in ``lambda`` mode the simulator with its code), then
        ``FastChannels-<prefix>-Pds`` with ``modules.pds.enabled`` and ``FastChannels-<prefix>-Release`` with
        ``modules.release.enabled``: they need the guidance's network."""
        demand = self.config.modules.demand
        key = None
        if demand.mode == "demo" and demand.simulator == "lambda":
            key = self.simulator_package(self.simulator_creatives(demo, content_base_url))
        network = self.network(key)
        self.check_pds_state()
        self.check_release_state()
        log(f"deploying {', '.join(self.beside_stacks())}")
        run(
            [
                CDK,
                "deploy",
                *self.beside_stacks(),
                "--exclusively",
                "--require-approval",
                "never",
                *self.repository_context(),
                "-c",
                f"{NETWORK_CONTEXT}={network.model_dump_json()}",
            ],
            env=self.cdk_environment(),
            stream=True,
        )

    def check_pds_state(self) -> None:
        """With ``modules.pds.enabled`` the Data and Media stacks must have written the parameters the Pds stack
        reads (``/<prefix>/pds/*``: its table's and buckets' names, the CDN, the ladder, and with ``modules.crawl``
        the catalog-context bucket; ``make deploy`` first); without it, a Pds stack left from before is deleted."""
        if not self.config.modules.pds.enabled:
            self.delete_stack(self.client("cloudformation"), self.pds_stack)
            return
        read = pds_names.stack_parameters(crawl=self.config.modules.crawl.enabled)
        wanted = [pds_names.parameter_name(self.prefix, parameter) for parameter in read]
        if missing := self.client("ssm").get_parameters(Names=wanted).get("InvalidParameters"):
            raise DemandError(
                f"the PDS's parameters {sorted(missing)} are missing: run `make deploy` with modules.pds.enabled first"
            )

    def check_release_state(self) -> None:
        """With ``modules.release.enabled`` the Media stack must publish what the Release stack reads (``make
        deploy`` first); without it, a Release stack left from before is deleted (its reaper deletes the siblings
        and Functions the controller created)."""
        if not self.config.modules.release.enabled:
            self.delete_stack(self.client("cloudformation"), self.release_stack)
            return
        name = release_names.parameter_name(self.prefix, release_names.Parameter.CONTENT_BASE_URL)
        try:
            self.client("ssm").get_parameter(Name=name)
        except ClientError:
            raise DemandError(
                f"no SSM parameter {name}: run `make deploy` with modules.release.enabled first"
            ) from None

    def start_census_gate(self) -> str:
        """Run the census gate once with the census's last count, so a stage deployed without running channels
        scales to zero (after the gate's 15 minutes) and one with channels keeps the tier minimum."""
        running = int(
            self.client("ssm").get_parameter(Name=f"/{self.prefix}/ops/running-channels")["Parameter"]["Value"]
        )
        arn = self.stack_outputs(self.demand_stack)["CensusGateArn"]
        detail = {"running": running, "previous": running, "correlation_id": "deploy-demand"}
        execution = self.client("stepfunctions").start_execution(
            stateMachineArn=arn, input=json.dumps({"detail": detail})
        )["executionArn"]
        log(f"census gate started with running={running}: {execution}")
        if self.config.modules.pds.enabled and (pds := self.stack_outputs(self.pds_stack).get("CensusGateArn")):
            gate = self.client("stepfunctions").start_execution(
                stateMachineArn=pds, input=json.dumps({"detail": detail})
            )["executionArn"]
            log(f"PDS census gate started with running={running}: {gate}")
        return str(execution)

    def warm_running_tasks(self, timeout_s: int = 1200) -> dict[str, str]:
        """Warm every running task of the guidance's service that no warm-up build has named yet, and wait for the
        builds; returns task ARN -> build status. A first deploy starts its tasks before the Demand stack and its
        warm-up rule exist, so the rule never sees them; tasks a later deploy starts are the rule's."""
        parameters = self.parameters()
        cluster, service = parameters[Parameter.CLUSTER_ARN], parameters[Parameter.SERVICE_ARN]
        ecs, codebuild = self.client("ecs"), self.client("codebuild")
        project = names.warmup_project(self.prefix)
        tasks = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING")["taskArns"]
        recent = codebuild.list_builds_for_project(projectName=project, sortOrder="DESCENDING")["ids"][:50]
        warmed = {
            variable["value"]
            for build in (codebuild.batch_get_builds(ids=recent)["builds"] if recent else [])
            for variable in build.get("environment", {}).get("environmentVariables", [])
            if variable["name"] == "TASK_ARN"
        }
        builds = {
            task: codebuild.start_build(
                projectName=project,
                environmentVariablesOverride=[{"name": "TASK_ARN", "value": task, "type": "PLAINTEXT"}],
            )["build"]["id"]
            for task in tasks
            if task not in warmed
        }
        log(f"warming {len(builds)} of {len(tasks)} running tasks ({len(tasks) - len(builds)} already warmed)")
        deadline = time.monotonic() + timeout_s
        states: dict[str, str] = {}
        while builds:
            found = codebuild.batch_get_builds(ids=list(builds.values()))["builds"]
            states = {task: next(b["buildStatus"] for b in found if b["id"] == build) for task, build in builds.items()}
            if all(status != "IN_PROGRESS" for status in states.values()) or time.monotonic() > deadline:
                break
            time.sleep(15)
        if failed := {task: status for task, status in states.items() if status != "SUCCEEDED"}:
            raise DemandError(f"warm-up builds did not succeed: {failed}")
        return states

    def herd(self, rate: int, seconds: int) -> dict[str, Any]:
        """A break herd through the guidance's CloudFront (the warm-up project with another rate and duration):
        vegeta's JSON report (status codes, latency percentiles in nanoseconds)."""
        codebuild, logs = self.client("codebuild"), self.client("logs")
        project = names.warmup_project(self.prefix)
        build = codebuild.start_build(
            projectName=project,
            environmentVariablesOverride=[
                {"name": "RATE", "value": str(rate), "type": "PLAINTEXT"},
                {"name": "SECONDS_TO_RUN", "value": str(seconds), "type": "PLAINTEXT"},
            ],
        )["build"]
        log(f"herd {rate}/s x {seconds} s: build {build['id']}")
        while (state := codebuild.batch_get_builds(ids=[build["id"]])["builds"][0])["buildStatus"] == "IN_PROGRESS":
            time.sleep(10)
        stream = state["logs"]["streamName"]
        events = logs.get_log_events(logGroupName=f"/aws/codebuild/{project}", logStreamName=stream, startFromHead=True)
        line = next((e["message"] for e in events["events"] if e["message"].startswith("VEGETA_JSON ")), None)
        if line is None:
            raise DemandError(f"herd build {build['id']} ended {state['buildStatus']} without a vegeta report")
        report: dict[str, Any] = json.loads(line.removeprefix("VEGETA_JSON "))
        codes = {str(k): int(v) for k, v in report.get("status_codes", {}).items()}
        total = sum(codes.values())
        latencies = report.get("latencies", {})
        return {
            "rate": rate,
            "seconds": seconds,
            "requests": total,
            "status_codes": codes,
            "non_200_ratio": (total - codes.get("200", 0)) / total if total else 1.0,
            "p50_ms": round(latencies.get("50th", 0) / 1e6, 1),
            "p99_ms": round(latencies.get("99th", 0) / 1e6, 1),
            "max_ms": round(latencies.get("max", 0) / 1e6, 1),
        }

    def simulator_function(self) -> str | None:
        """The simulator's function name for the mode, or None (no simulator)."""
        demand = self.config.modules.demand
        if demand.mode != "demo":
            return None
        if demand.simulator == "lambda":
            return names.simulator_function(self.prefix)
        cloudformation = self.client("cloudformation")
        for page in cloudformation.get_paginator("list_stack_resources").paginate(
            StackName=names.simulator_stack(self.prefix)
        ):
            for row in page["StackResourceSummaries"]:
                if row["ResourceType"] == "AWS::Lambda::Function" and row["LogicalResourceId"].startswith(
                    "bidderSimulator"
                ):
                    return str(row["PhysicalResourceId"])
        raise DemandError(f"no bidder simulator function in {names.simulator_stack(self.prefix)}")

    def simulator_smoke(self, demo: DemoIndex | None) -> dict[str, Any]:
        """A direct 4-slot pod request to the simulator, as Prebid Server's ``amt`` adapter sends it through the
        load balancer: one bid per slot, each with a demo creative when the stage has them."""
        function = self.simulator_function()
        assert function is not None
        request = {
            "id": "smoke",
            "imp": [
                {"id": f"s{n}", "video": {"mimes": ["application/x-mpegURL"], "rqddurs": [30], "podid": 1}}
                for n in range(1, SIMULATOR_SLOTS + 1)
            ],
        }
        event = {
            "httpMethod": "POST",
            "path": "/",
            "headers": {"content-type": "application/json"},
            "body": json.dumps(request),
            "isBase64Encoded": False,
        }
        answer = json.loads(
            self.client("lambda").invoke(FunctionName=function, Payload=json.dumps(event).encode())["Payload"].read()
        )
        bids = [bid for seat in json.loads(answer.get("body") or "{}").get("seatbid", []) for bid in seat["bid"]]
        expected = {c.crid for c in demo.creatives if c.role is DemoRole.SIMULATOR} if demo else None
        crids = [str(bid.get("crid")) for bid in bids]
        return {
            "function": function,
            "status": answer.get("statusCode"),
            "bids": [
                {
                    "impid": b.get("impid"),
                    "crid": b.get("crid"),
                    "adomain": b.get("adomain"),
                    "price": b.get("price"),
                    "dur": b.get("dur"),
                }
                for b in bids
            ],
            "passed": answer.get("statusCode") == 200
            and sorted(str(b.get("impid")) for b in bids) == [f"s{n}" for n in range(1, SIMULATOR_SLOTS + 1)]
            and (expected is None or set(crids) <= expected),
        }

    def demo_creatives_smoke(self, demo: DemoIndex) -> dict[str, Any]:
        """Every demo creative's multivariant, through the CDN, has the ladder's variants in order: codec, size and
        frame rate (``ladder_signature``) and the nominal ``BANDWIDTH``."""
        outputs = self.media_outputs() or {}
        ladder = next(
            (
                Ladder.builtin(n)
                for n in self.config.ladders.builtin
                if Ladder.builtin(n).version == demo.ladder_version
            ),
            None,
        )
        results: dict[str, Any] = {}
        with httpx.Client(timeout=10, follow_redirects=True) as http:
            for creative in demo.creatives:
                text = http.get(creative.hls_uri).text
                variants = parse_multivariant(text)
                row: dict[str, Any] = {"variants": len(variants)}
                if ladder is not None:
                    row["signature"] = ladder_signature(text) == expected_signature(ladder)
                    row["bandwidth"] = [v.bandwidth for v in variants] == [
                        ladder.nominal_bandwidth(r)[0] for r in ladder.video
                    ]
                results[creative.crid] = row
        passed = ladder is not None and all(r["signature"] and r["bandwidth"] for r in results.values())
        return {
            "ladder": demo.ladder_version,
            "content_base_url": outputs.get("ContentBaseUrl"),
            **results,
            "passed": passed,
        }

    # ----- outputs and configuration -------------------------------------------------------------------------------

    def discover(self, image: str | None = None) -> dict[Parameter, str]:
        """The SSM contract, read from the deployed guidance."""
        stack = names.guidance_stack(self.prefix)
        outputs = self.stack_outputs(stack)
        cluster, service = outputs["EcsClusterName"], outputs["EcsServiceName"]
        ecs = self.client("ecs")
        [described] = ecs.describe_services(cluster=cluster, services=[service])["services"]
        network = described["networkConfiguration"]["awsvpcConfiguration"]
        ec2 = self.client("ec2")
        [subnet] = ec2.describe_subnets(SubnetIds=network["subnets"][:1])["Subnets"]
        elb = self.client("elbv2")
        [group] = elb.describe_target_groups(TargetGroupArns=[described["loadBalancers"][0]["targetGroupArn"]])[
            "TargetGroups"
        ]
        domain = outputs["PrebidCloudFrontDistributionEndpoint"]
        cloudfront = self.client("cloudfront", "us-east-1")
        distributions = cloudfront.list_distributions()["DistributionList"].get("Items", [])
        [distribution] = [d for d in distributions if d["DomainName"] == domain]
        config_output = next(v for k, v in outputs.items() if k.startswith("ContainerImagePrebidSolutionConfig"))
        settings = console_bucket(config_output)
        stored = console_bucket(outputs["PrebidStoredRequestsBucket"])
        [running] = [d for d in described["deployments"] if d["status"] == "PRIMARY"]
        definition = ecs.describe_task_definition(taskDefinition=running["taskDefinition"])["taskDefinition"]
        pin = json.loads((self.checkout / ".fast-overlays.json").read_text())
        return {
            Parameter.CLOUDFRONT_DOMAIN: domain,
            Parameter.SETTINGS_BUCKET: settings,
            Parameter.STORED_REQUESTS_BUCKET: stored,
            Parameter.CLUSTER_ARN: described["clusterArn"],
            Parameter.SERVICE_ARN: described["serviceArn"],
            Parameter.TASK_SECURITY_GROUP: network["securityGroups"][0],
            Parameter.VPC_ID: subnet["VpcId"],
            Parameter.PRIVATE_SUBNET_IDS: ",".join(network["subnets"]),
            Parameter.ALB_ARN: group["LoadBalancerArns"][0],
            Parameter.WEB_ACL_ARN: distribution["WebACLId"],
            Parameter.IMAGE_URI: image or definition["containerDefinitions"][0]["image"],
            Parameter.GUIDANCE: f"{pin['tag']}@{pin['commit']}",
        }

    def write_parameters(self, values: Mapping[Parameter, str]) -> None:
        ssm = self.client("ssm")
        for parameter, value in values.items():
            ssm.put_parameter(
                Name=names.parameter_path(self.prefix, parameter), Value=value, Type="String", Overwrite=True
            )
            ssm.add_tags_to_resource(
                ResourceType="Parameter",
                ResourceId=names.parameter_path(self.prefix, parameter),
                Tags=[{"Key": k, "Value": v} for k, v in self.tags().items()],
            )
        log(f"wrote {len(values)} parameters under {names.parameter_path(self.prefix)}")

    def parameters(self) -> dict[str, str]:
        ssm = self.client("ssm")
        found: dict[str, str] = {}
        for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=names.parameter_path(self.prefix)):
            found |= {p["Name"].rsplit("/", 1)[1]: p["Value"] for p in page["Parameters"]}
        return found

    def upload_config(self, content_base_url: str) -> None:
        """Upload the rendered configuration (``current/`` and the stored data) and force a new deployment, which
        overlay O7 keeps at full capacity."""
        from prebid_guidance import pbs_settings  # noqa: PLC0415

        values = self.parameters()
        s3 = self.client("s3")
        for rel, content in render(pbs_settings(self.config, content_base_url)).items():
            if rel.startswith("current-config/"):
                bucket, key = values[Parameter.SETTINGS_BUCKET], f"prebid-server/current/{rel.split('/', 1)[1]}"
            else:
                bucket, key = values[Parameter.STORED_REQUESTS_BUCKET], rel
            s3.put_object(Bucket=bucket, Key=key, Body=content.encode())
        ecs = self.client("ecs")
        cluster, service = values[Parameter.CLUSTER_ARN], values[Parameter.SERVICE_ARN]
        ecs.update_service(cluster=cluster, service=service, forceNewDeployment=True)
        log("configuration uploaded; waiting for the new deployment")
        ecs.get_waiter("services_stable").wait(
            cluster=cluster, services=[service], WaiterConfig={"Delay": 15, "MaxAttempts": 80}
        )

    # ----- smoke ---------------------------------------------------------------------------------------------------

    def origin_secret(self, slot: str = "a") -> str:
        secrets = self.client("secretsmanager", "us-east-1")
        value: str = secrets.get_secret_value(SecretId=names.origin_secret(self.prefix, slot))["SecretString"]
        return value

    def smoke(self) -> dict[str, Any]:
        """A synthetic MediaTailor POST per grid duration (VAST 4.2, ``application/xml``), one without the origin
        secret (blocked at the edge), one for an off-grid duration (400) and one for another path (blocked)."""
        domain = self.parameters()[Parameter.CLOUDFRONT_DOMAIN]
        secret = self.origin_secret()
        results: dict[str, Any] = {}
        with httpx.Client(timeout=10) as http:

            def post(seconds: int, headers: Mapping[str, str]) -> httpx.Response:
                body = {"ext": {"prebid": {"storedrequest": {"id": f"fast-pod-{seconds}s"}}}}
                query = f"evid=1&upid=smoke&dur={seconds * 1001}"
                sent = {
                    "Content-Type": "application/json",
                    "X-Fast-Sid": f"smoke-{seconds}",
                    "X-Fast-Probe": "1",
                    "X-Fast-Pc": f"{self.prefix}-smoke",
                    **headers,
                }
                return http.post(f"https://{domain}/openrtb2/auction?{query}", json=body, headers=sent)

            for seconds in POD_SECONDS:
                response = post(seconds, {"X-Fast-Origin": secret})
                results[f"{seconds}s"] = {
                    "status": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "vast_4_2": response.text.startswith('<VAST version="4.2"'),
                }
            results["no-secret"] = {"status": post(60, {}).status_code}
            results["off-grid-31s"] = {"status": post(31, {"X-Fast-Origin": secret}).status_code}
            results["other-path"] = {"status": http.get(f"https://{domain}/info/bidders").status_code}
        if self.config.modules.demand.mode == "demo":
            demo = self.demo_index()
            results["simulator"] = self.simulator_smoke(demo)
            if demo is not None:
                results["demo-creatives"] = self.demo_creatives_smoke(demo)
        results["passed"] = smoke_passed(results)
        return results

    # ----- destroy and inventory -----------------------------------------------------------------------------------

    def retained(self) -> Retained:
        found = Retained()
        cloudformation = self.client("cloudformation")
        for stack in (names.guidance_stack(self.prefix), names.simulator_stack(self.prefix)):
            try:
                # ListStackResources pages; DescribeStackResources stops at 100 of the guidance's ~350 resources
                resources = [
                    row
                    for page in cloudformation.get_paginator("list_stack_resources").paginate(StackName=stack)
                    for row in page["StackResourceSummaries"]
                ]
            except ClientError:
                continue
            outputs = self.stack_outputs(stack)
            found = found.merge(retained_resources(resources, outputs.get("EcsClusterName")))
        return found

    def destroy(self) -> None:
        """Every guidance stack, what its templates retain, the SSM contract, the bootstrap and the repository's
        stacks, in that order; KMS keys go to their 7-day deletion window."""
        record = BUILD / f"{self.prefix}-retained.json"
        known = Retained(**json.loads(record.read_text())) if record.is_file() else Retained()
        retained = known.merge(self.retained())
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(retained.__dict__, indent=2))
        cloudformation = self.client("cloudformation")
        for stack in (
            self.release_stack,
            self.pds_stack,
            self.demand_stack,
            names.guidance_stack(self.prefix),
            names.simulator_stack(self.prefix),
        ):
            self.delete_stack(cloudformation, stack)
        self.remove_retained(retained)
        self.delete_parameters()
        self.destroy_bootstrap()
        self.destroy_repository_stacks()
        record.unlink(missing_ok=True)

    def delete_stack(self, cloudformation: Any, stack: str) -> None:
        try:
            cloudformation.describe_stacks(StackName=stack)
        except ClientError:
            return
        log(f"deleting {stack}")
        cloudformation.delete_stack(StackName=stack)
        cloudformation.get_waiter("stack_delete_complete").wait(
            StackName=stack, WaiterConfig={"Delay": 20, "MaxAttempts": 270}
        )

    def remove_retained(self, retained: Retained) -> None:
        s3, logs, kms, ecs = self.client("s3"), self.client("logs"), self.client("kms"), self.client("ecs")
        for bucket in retained.buckets:
            try:
                log(f"emptying and deleting bucket {bucket} ({empty_bucket(s3, bucket)} versions)")
                s3.delete_bucket(Bucket=bucket)
            except ClientError as error:
                if error.response["Error"]["Code"] != "NoSuchBucket":
                    raise
        for group in retained.log_groups:
            with suppress(logs.exceptions.ResourceNotFoundException):
                logs.delete_log_group(logGroupName=group)
        for key in retained.keys:
            try:
                state = kms.describe_key(KeyId=key)["KeyMetadata"]
            except kms.exceptions.NotFoundException:
                continue
            if state["KeyState"] == "PendingDeletion":
                if state["DeletionDate"] - datetime.now(UTC) <= timedelta(days=KMS_PENDING_DAYS + 1):
                    continue
                kms.cancel_key_deletion(KeyId=key)  # scheduled with a longer window; the minimum is 7 days
            kms.schedule_key_deletion(KeyId=key, PendingWindowInDays=KMS_PENDING_DAYS)
            log(f"key {key} scheduled for deletion in {KMS_PENDING_DAYS} days")
        for definition in retained.task_definitions:
            with suppress(ClientError):
                ecs.deregister_task_definition(taskDefinition=definition)
        for start in range(0, len(retained.task_definitions), 10):
            with suppress(ClientError):
                ecs.delete_task_definitions(taskDefinitions=retained.task_definitions[start : start + 10])

    def delete_parameters(self) -> None:
        ssm = self.client("ssm")
        found = [names.parameter_path(self.prefix, Parameter(name)) for name in self.parameters()]
        for start in range(0, len(found), 10):  # DeleteParameters takes at most 10 names
            ssm.delete_parameters(Names=found[start : start + 10])

    def destroy_bootstrap(self) -> None:
        bucket = names.assets_bucket(self.prefix, self.account, self.region)
        repository = names.images_repository(self.prefix, self.account, self.region)
        self.delete_stack(self.client("cloudformation"), names.toolkit_stack(self.prefix))
        s3, ecr = self.client("s3"), self.client("ecr")
        try:
            empty_bucket(s3, bucket)
            s3.delete_bucket(Bucket=bucket)
        except ClientError as error:
            if error.response["Error"]["Code"] != "NoSuchBucket":
                raise
        with suppress(ecr.exceptions.RepositoryNotFoundException):
            ecr.delete_repository(repositoryName=repository, force=True)

    def inventory(self) -> list[str]:
        """What is left of the stage's demand module; KMS keys pending deletion and the tagging index's stale
        entries for deleted resources are not residue."""
        residue = []
        cloudformation = self.client("cloudformation")
        for stack in (
            self.release_stack,
            self.pds_stack,
            self.demand_stack,
            names.guidance_stack(self.prefix),
            names.simulator_stack(self.prefix),
            names.toolkit_stack(self.prefix),
            self.build_stack,
        ):
            try:
                [described] = cloudformation.describe_stacks(StackName=stack)["Stacks"]
                residue.append(f"stack {stack} {described['StackStatus']}")
            except ClientError:
                pass
        try:
            self.client("cloudformation", "us-east-1").describe_stacks(StackName=self.edge_stack)
            residue.append(f"stack {self.edge_stack}")
        except ClientError:
            pass
        for region in sorted({self.region, "us-east-1"}):
            tagging = self.client("resourcegroupstaggingapi", region)
            for page in tagging.get_paginator("get_resources").paginate(
                TagFilters=[
                    {"Key": "fast:app", "Values": [self.prefix]},
                    {"Key": names.MODULE_TAG, "Values": [names.MODULE, pds_names.MODULE, release_names.MODULE]},
                ]
            ):
                residue += [
                    arn
                    for item in page["ResourceTagMappingList"]
                    if (arn := item["ResourceARN"]) and not self.gone(arn, region)
                ]
        s3 = self.client("s3")
        residue += [
            f"bucket {b['Name']}"
            for b in s3.list_buckets()["Buckets"]
            if b["Name"].startswith((f"{self.prefix}-pbs", f"{self.prefix}-cdk-"))
        ]
        logs = self.client("logs")
        for prefix in (f"/aws/lambda/{self.prefix}-pbs", f"{self.prefix}-pbs", f"/aws/codebuild/{self.prefix}-pbs"):
            for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
                residue += [f"log group {g['logGroupName']}" for g in page["logGroups"]]
        if self.parameters():
            residue.append(f"parameters under {names.parameter_path(self.prefix)}")
        return sorted(set(residue))

    def gone(self, arn: str, region: str) -> bool:
        """Whether a tagged ARN is a deleted resource the tagging index still lists, or a key pending deletion."""
        service = arn.split(":")[2]
        try:
            match service:
                case "kms":
                    state = self.client("kms", region).describe_key(KeyId=arn)["KeyMetadata"]["KeyState"]
                    return bool(state == "PendingDeletion")
                case "ec2":
                    return self.ec2_gone(arn, region)
                case "ecs":
                    return self.ecs_gone(arn, region)
        except ClientError:
            return True
        return False

    def ec2_gone(self, arn: str, region: str) -> bool:
        ec2, resource_id = self.client("ec2", region), arn.rsplit("/", 1)[1]
        kind = arn.split(":")[5].split("/", maxsplit=1)[0]
        if kind == "natgateway":
            [nat] = ec2.describe_nat_gateways(NatGatewayIds=[resource_id])["NatGateways"]
            return bool(nat["State"] == "deleted")
        if kind == "vpc-peering-connection":
            # A deleted peering connection stays visible for a while, with that status
            [peering] = ec2.describe_vpc_peering_connections(VpcPeeringConnectionIds=[resource_id])[
                "VpcPeeringConnections"
            ] or [{"Status": {"Code": "deleted"}}]
            return bool(peering["Status"]["Code"] in ("deleted", "rejected", "failed", "expired"))
        if kind in EC2_DESCRIBE:
            operation, key = EC2_DESCRIBE[kind]
            found = getattr(ec2, operation)(**{key: [resource_id]})
            return not any(isinstance(value, list) and value for value in found.values())
        return False

    def ecs_gone(self, arn: str, region: str) -> bool:
        ecs = self.client("ecs", region)
        if ":task-definition/" in arn:
            [definition] = [ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"]]
            return bool(definition["status"] in ("INACTIVE", "DELETE_IN_PROGRESS"))
        if ":cluster/" in arn:
            [cluster] = ecs.describe_clusters(clusters=[arn])["clusters"] or [{"status": "INACTIVE"}]
            return bool(cluster["status"] == "INACTIVE")
        if ":task/" in arn:
            return True  # stopped tasks stay listed for a while
        if ":service/" in arn:
            cluster = arn.split("/")[1]
            [service] = ecs.describe_services(cluster=cluster, services=[arn])["services"] or [{"status": "INACTIVE"}]
            return bool(service["status"] == "INACTIVE")
        return False


def iterate_tool_versions() -> Iterator[tuple[str, str]]:
    """The tools a demand deploy uses, and their versions."""
    for name, argv in (
        ("git", ["git", "--version"]),
        ("uv", ["uv", "--version"]),
        ("node", ["node", "--version"]),
        ("cdk", [str(CDK), "--version"]),
        (
            "aws credentials",
            [sys.executable, "-c", "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"],
        ),
    ):
        try:
            yield name, run(argv, env={**os.environ, "JSII_SILENCE_WARNING_UNTESTED_NODE_VERSION": "1"}).strip()
        except (DemandError, FileNotFoundError) as error:
            yield name, f"MISSING: {error}"
