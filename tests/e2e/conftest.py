"""Fixtures of the deployed end-to-end suite: ``uv run pytest tests/e2e -m e2e --prefix <prefix>`` (README.md).

Session fixtures do the slow, billable steps once and in dependency order: ingest, plan and publish, start the
channel, watch it air. Every e2e test is skipped when the stage is not deployed, and the channel is stopped when
the session ends, whatever failed. One run id is the ingest correlation id and the correlation id of every tool
call, so ``scripts/trace.py <run id>`` shows the whole run.
"""

import json
import sys
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import uuid4

import pytest
from boto3.dynamodb.conditions import Key
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

HERE: Final = Path(__file__).resolve().parent
# _stack (shared with the scripts) and the helpers; appended, so that scripts/trace.py never shadows stdlib trace.
sys.path += [str(HERE.parents[1] / "scripts"), str(HERE)]

from _stack import Plane, StackError, Stage, configured_prefix  # noqa: E402
from harness import (  # noqa: E402
    ASSET_ID,
    BREAK_POLICY,
    CONTENT_ID,
    CUE_POINT_MS,
    LEAD,
    PATTERN_SECONDS,
    PROGRAMS,
    SPEECH,
    SPOKEN_ASSET_ID,
    SPOKEN_SECONDS,
    Channel,
    Observation,
    Player,
    Tools,
    render_captions,
    render_speech,
    render_test_pattern,
    until,
    wait_for,
    watch,
)
from playback import break_windows  # noqa: E402

from fast_channels.core.ingest import AssetKind  # noqa: E402
from fast_channels.functions.aws import execution_arn  # noqa: E402
from fast_channels.functions.tools.library_store import (  # noqa: E402
    GSI1,
    Asset,
    AssetStatus,
    LadderRecord,
    asset_key,
    ladder_key,
)

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.service_resource import Table

    from fast_channels.core.ladder import Ladder

RUN_ID: Final = f"e2e-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid4().hex[:6]}"
"""Correlation id of the run (printed in the report header): the ingest execution name, and every tool call's."""
INGEST_TIMEOUT_S: Final = 1200
EXECUTION_GRACE_S: Final = 180
"""How long the upload may go without an ingest execution (the trigger rejects invalid requests instead)."""
FAILED_EXECUTION: Final = frozenset({"FAILED", "TIMED_OUT", "ABORTED"})


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--prefix", help="deployment prefix of the stage under test (default: fast-channels.prefix in cdk.json)"
    )


def pytest_report_header(config: pytest.Config) -> str:
    return f"e2e run id: {RUN_ID} (uv run python scripts/trace.py {RUN_ID})"


@pytest.fixture(scope="session")
def stage(request: pytest.FixtureRequest) -> Stage:
    """The deployed stage; skips the e2e tests when its stacks (or AWS credentials) are missing."""
    prefix = request.config.getoption("prefix") or configured_prefix()
    try:
        stage = Stage.of(prefix)
        missing = [stage.stack_name(plane) for plane in Plane if stage.status(plane) is None]
    except (BotoCoreError, ClientError, StackError) as error:
        pytest.skip(f"cannot read the stacks of stage {prefix}: {error}")
    if missing:
        pytest.skip(f"stage {stage.prefix} is not deployed in {stage.region} (no {', '.join(missing)})")
    return stage


@pytest.fixture(scope="session")
def run_id() -> str:
    return RUN_ID


@pytest.fixture(scope="session")
def channel(stage: Stage) -> Channel:
    """The stage's first IaC channel."""
    channels = stage.json_output(Plane.MEDIA, "Channels")
    if not channels:
        pytest.fail("the stage has no IaC channel (cdk.json fast-channels.channels.iac)")
    return Channel.of(channels[0])


@pytest.fixture(scope="session")
def library(stage: Stage) -> Table:
    dynamodb = stage.session.resource("dynamodb", region_name=stage.region)
    return dynamodb.Table(stage.output(Plane.DATA, "LibraryTableName"))


@pytest.fixture(scope="session")
def ladder(library: Table, channel: Channel) -> Ladder:
    """The definition of the channel's ladder version."""
    item = library.get_item(Key=ladder_key(channel.ladder_version)).get("Item")
    assert item, f"no LADDER item for {channel.ladder_version} in {library.name}"
    return LadderRecord.from_item(item).definition()


@pytest.fixture(scope="session")
def tools(stage: Stage, run_id: str) -> Tools:
    # A synchronous tool call can take minutes (start_channel waits for RUNNING), but never longer than the tool
    # functions' 5-minute timeout. TCP keepalive stops an idle connection from being dropped silently between calls
    # (observed: a response that never arrived, then a 900 s read timeout). Never re-send a call on a timeout.
    config = Config(read_timeout=330, tcp_keepalive=True, retries={"mode": "standard", "total_max_attempts": 1})
    client = stage.session.client("lambda", region_name=stage.region, config=config)
    return Tools(client, stage.json_output(Plane.OPERATOR, "ToolFunctions"), run_id)


@pytest.fixture(scope="session")
def player() -> Iterator[Player]:
    player = Player()
    yield player
    player.close()


@pytest.fixture(scope="session")
def ingested(
    stage: Stage, library: Table, channel: Channel, run_id: str, tmp_path_factory: pytest.TempPathFactory
) -> Asset:
    """The synthetic source with its caption file (a drop-folder sibling, ``<asset>.en.vtt``), uploaded to the
    ingest bucket after its sidecar, once its encode is READY."""
    folder = tmp_path_factory.mktemp("source")
    source = render_test_pattern(folder / f"{ASSET_ID}.mp4", seconds=PATTERN_SECONDS)
    captions = render_captions(folder / f"{ASSET_ID}.en.vtt", seconds=PATTERN_SECONDS)
    sidecar = {
        "asset_id": ASSET_ID,
        "content_id": CONTENT_ID,
        "cue_points_ms": [CUE_POINT_MS],
        "ladder_version": channel.ladder_version,
        "correlation_id": run_id,
        "metadata": {"title": "End-to-end test pattern"},
    }
    return upload_and_wait(stage, library, source, sidecar, siblings=[captions])


@pytest.fixture(scope="session")
def spoken(
    stage: Stage, library: Table, channel: Channel, run_id: str, tmp_path_factory: pytest.TempPathFactory
) -> Asset:
    """A test pattern that speaks (Amazon Polly) without captions, once its encode is READY: the captioner made
    its English track. Skipped on stages that never generate captions (output ``CaptionEngine`` ``none``)."""
    if stage.output(Plane.MEDIA, "CaptionEngine") == "none":
        pytest.skip(f"stage {stage.prefix} does not generate captions (fast-channels.captions.engine=none)")
    folder = tmp_path_factory.mktemp("spoken")
    polly = stage.session.client("polly", region_name=stage.region)
    speech = polly.synthesize_speech(Engine="neural", VoiceId="Joanna", OutputFormat="mp3", Text=SPEECH)
    (audio := folder / "speech.mp3").write_bytes(speech["AudioStream"].read())
    source = render_speech(folder / f"{SPOKEN_ASSET_ID}.mp4", audio, seconds=SPOKEN_SECONDS)
    sidecar = {
        "asset_id": SPOKEN_ASSET_ID,
        "content_id": f"{CONTENT_ID}-spoken",
        "ladder_version": channel.ladder_version,
        "correlation_id": f"{run_id}-spoken",
        "captions": {"spoken_language": "en"},
        "metadata": {"title": "End-to-end speaking test pattern"},
    }
    return upload_and_wait(stage, library, source, sidecar)


def upload_and_wait(
    stage: Stage, library: Table, source: Path, sidecar: dict[str, Any], *, siblings: Sequence[Path] = ()
) -> Asset:
    """Upload ``siblings`` and the JSON sidecar, then ``source`` under ``e2e/``; the asset once READY."""
    bucket, key = stage.output(Plane.DATA, "IngestBucketName"), f"e2e/{source.name}"
    asset_id, ladder_version, correlation_id = sidecar["asset_id"], sidecar["ladder_version"], sidecar["correlation_id"]
    s3 = stage.session.client("s3", region_name=stage.region)
    for sibling in siblings:
        s3.upload_file(str(sibling), bucket, f"e2e/{sibling.name}")
    s3.put_object(Bucket=bucket, Key=f"{key}.json", Body=json.dumps(sidecar).encode(), ContentType="application/json")
    s3.upload_file(str(source), bucket, key)

    sfn = stage.session.client("stepfunctions", region_name=stage.region)
    execution = execution_arn(stage.output(Plane.MEDIA, "IngestStateMachineArn"), correlation_id)
    uploaded = time.monotonic()
    last: dict[str, Any] = {}

    def ready() -> Asset | None:
        item = library.get_item(Key=asset_key(asset_id, ladder_version)).get("Item")
        asset = Asset.from_item(item) if item else None
        if asset is not None and asset.correlation_id == correlation_id:
            last["asset"] = asset.status
            if asset.status is AssetStatus.READY:
                return asset
            if asset.status is AssetStatus.FAILED:
                pytest.fail(f"ingest {correlation_id} FAILED: {asset.error} (scripts/trace.py {correlation_id})")
        try:
            described = sfn.describe_execution(executionArn=execution)
        except sfn.exceptions.ExecutionDoesNotExist:
            if time.monotonic() - uploaded > EXECUTION_GRACE_S:
                pytest.fail(f"no ingest execution {correlation_id} for s3://{bucket}/{key}: was it rejected?")
            return None
        last["execution"] = described["status"]
        if described["status"] in FAILED_EXECUTION:
            cause = f"{described.get('error', '')}: {described.get('cause', '')}"[:500]
            pytest.fail(f"ingest {correlation_id} {described['status']} ({cause}; scripts/trace.py {correlation_id})")
        return None

    return wait_for(ready, timeout=INGEST_TIMEOUT_S, interval=15, what=f"{asset_id} READY", state=lambda: last)


@pytest.fixture(scope="session")
def plan(tools: Tools, channel: Channel, ingested: Asset) -> dict[str, Any]:
    """``schedule___plan``: the test pattern three times, starting in a few minutes, one break per program."""
    start = (datetime.now(UTC) + LEAD).replace(microsecond=0)
    return tools(
        "schedule___plan",
        channel=channel.name,
        picks=[ingested.asset_id] * PROGRAMS,
        start=start.isoformat(),
        break_policy=BREAK_POLICY,
    )


@pytest.fixture(scope="session")
def published(tools: Tools, plan: dict[str, Any]) -> dict[str, Any]:
    """``schedule___publish`` of the plan: the EPG entries of the published block."""
    assert plan["publishable"], f"plan {plan['plan_id']} has errors: {plan['violations']}"
    return tools("schedule___publish", plan_id=plan["plan_id"])


@pytest.fixture(scope="session")
def running(tools: Tools, channel: Channel, published: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """``channels___start_channel`` once the schedule is published; stopped at the end of the session."""
    try:
        yield tools("channels___start_channel", channel=channel.name)
    finally:
        tools("channels___stop_channel", channel=channel.name)


@pytest.fixture(scope="session")
def observation(
    player: Player,
    channel: Channel,
    library: Table,
    ladder: Ladder,
    published: dict[str, Any],
    running: dict[str, Any],
) -> Observation:
    """SSAI, SGAI and DASH sessions watched from the first program until every break was seen filled (or until
    shortly after the last break)."""
    breaks = break_windows(published["programs"])
    assert len(breaks) >= 2, f"two breaks are needed (the first one airs its slate): {published['programs']}"
    until(datetime.fromisoformat(published["programs"][0]["start"]) - timedelta(seconds=10))
    return watch(
        player,
        ssai_url=channel.url("ssai"),
        sgai_url=channel.url("sgai"),
        dash_url=channel.url("dash"),
        breaks=breaks,
        ends=breaks[-1].ends + timedelta(seconds=45),
        house_ads=house_ad_assets(library, ladder.version),
    )


def house_ad_assets(library: Table, ladder_version: str) -> frozenset[str]:
    """Asset ids of the ladder kit's house ads (media-library assets of kind ``ad``)."""
    condition = Key("gsi1pk").eq(f"KIND#{AssetKind.AD}#{ladder_version}")
    items = library.query(IndexName=GSI1, KeyConditionExpression=condition)["Items"]
    return frozenset(Asset.from_item(item).asset_id for item in items)
