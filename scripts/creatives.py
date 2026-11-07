#!/usr/bin/env -S uv run python
"""Register an advertiser's creative with the creatives module, and decide on it (``modules.creatives``).

    uv run python scripts/creatives.py register --prefix fastm --advertiser "Acme Outdoor" \\
        --adomain acme-outdoor.example --file spot.mp4 --wait          # a local MP4: uploaded to the quarantine
    uv run python scripts/creatives.py register --prefix fastm --advertiser "Acme Outdoor" \\
        --adomain acme-outdoor.example --url https://ads.example/vast.xml   # an https MP4 or VAST document
    uv run python scripts/creatives.py status --prefix fastm [<crid>]
    uv run python scripts/creatives.py approve --prefix fastm <crid> --note "brand-safe"   # or reject, suspend, resume

``OPTIMIZATION(aws): W-10 item 6``: the AWS reference buyer has no creative tasks (AdCP ``sync_creatives``), so an
operator registers a brand's creatives here; a buy booked over AdCP for that brand is then served with them
(docs/creatives.md). Each command starts the readiness state machine (``<prefix>-creatives``): the same input is the
path for an agent with ``states:StartExecution`` on it. A local file goes to the module's quarantine bucket, never to
the ingest bucket (whose upload rule would ingest it as content). ``--wait`` follows the execution and prints the
creative's state.
"""

import argparse
import hashlib
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from _stack import Module, Stage, add_prefix_argument, run

from fast_channels.core.creatives.names import crid_for, source_key
from fast_channels.core.creatives.registry import Action, Decision, Registration

POLL_SECONDS: Final = 5.0
TERMINAL: Final = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"})


class Creatives:
    """The creatives module of one deployment: its state machine, registry and quarantine bucket."""

    def __init__(self, stage: Stage) -> None:
        self.stage = stage
        self.state_machine = stage.output(Module.CREATIVES, "StateMachineArn")
        self.table = stage.output(Module.CREATIVES, "RegistryTable")
        self.bucket = stage.output(Module.CREATIVES, "QuarantineBucket")
        self.states = stage.session.client("stepfunctions", region_name=stage.region)
        self.s3 = stage.session.client("s3", region_name=stage.region)
        self.dynamodb = stage.session.resource("dynamodb", region_name=stage.region)

    def start(self, document: Registration | Decision) -> str:
        """Start an execution for ``document``; returns its ARN."""
        name = f"{document.crid}-{document.action}-{datetime.now(UTC):%Y%m%dT%H%M%S%f}"[:80]
        started = self.states.start_execution(
            stateMachineArn=self.state_machine, name=name, input=document.model_dump_json()
        )
        return str(started["executionArn"])

    def register_file(self, path: Path, *, advertiser: str, adomain: str, by: str) -> tuple[str, str]:
        """Upload a local MP4 to the quarantine and register it; returns (crid, execution ARN)."""
        data = path.read_bytes()
        if data[4:8] != b"ftyp":
            raise SystemExit(f"error: {path} is not an MP4")
        crid = crid_for(adomain, hashlib.sha256(data).hexdigest())
        key = source_key(crid)
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType="video/mp4")
        source = f"s3://{self.bucket}/{key}"
        return crid, self.start(Registration(crid=crid, advertiser=advertiser, adomain=adomain, source=source, by=by))

    def register_url(self, url: str, *, advertiser: str, adomain: str, by: str) -> tuple[str, str]:
        """Register an MP4 or a VAST document at ``url``; returns (crid, execution ARN)."""
        crid = crid_for(adomain, url)
        return crid, self.start(Registration(crid=crid, advertiser=advertiser, adomain=adomain, source=url, by=by))

    def wait(self, execution: str, *, timeout: float = 1800) -> dict[str, Any]:
        """Follow ``execution`` to its end; returns DescribeExecution's answer."""
        deadline = time.monotonic() + timeout
        while True:
            described = self.states.describe_execution(executionArn=execution)
            if described["status"] in TERMINAL or time.monotonic() > deadline:
                return dict(described)
            time.sleep(POLL_SECONDS)

    def record(self, crid: str) -> dict[str, Any] | None:
        """The registry item of ``crid``, or None."""
        found: dict[str, Any] | None = (
            self.dynamodb.Table(self.table).get_item(Key={"crid": crid}, ConsistentRead=True).get("Item")
        )
        return found

    def records(self) -> list[dict[str, Any]]:
        """Every registered creative (the registry is small: a scan)."""
        table, items = self.dynamodb.Table(self.table), []
        request: dict[str, Any] = {}
        while True:
            page = table.scan(**request)
            items += page.get("Items", [])
            if not (start := page.get("LastEvaluatedKey")):
                return sorted(items, key=lambda i: str(i.get("created_at", "")))
            request = {"ExclusiveStartKey": start}


def summary(record: dict[str, Any] | None) -> dict[str, Any]:
    """What ``status`` prints of a registry item."""
    if record is None:
        return {"state": None}
    keys = ("crid", "state", "advertiser", "adomain", "duration_ms", "creative_id", "hls_uri", "ladder_version")
    history = [
        f"{h['at']} {h['state']} by {h['by']}" + (f": {h['note']}" if h.get("note") else "")
        for h in record.get("history", [])
    ]
    return {key: _plain(record[key]) for key in keys if key in record} | {"history": history}


def _plain(value: Any) -> Any:
    """A number the DynamoDB resource read (a Decimal) as an int or a float."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="register a creative and run it to READY")
    add_prefix_argument(register)
    register.add_argument("--advertiser", required=True)
    register.add_argument("--adomain", required=True, help="the brand's domain (OpenRTB adomain)")
    source = register.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path, help="a local MP4")
    source.add_argument("--url", help="an https MP4 or VAST document")
    register.add_argument("--by", default="operator", help="who registers it (the registry's history)")
    register.add_argument("--wait", action="store_true", help="follow the execution and print the state")
    status = commands.add_parser("status", help="a creative's state and history, or every creative's state")
    add_prefix_argument(status)
    status.add_argument("crid", nargs="?")
    for action in (Action.APPROVE, Action.REJECT, Action.SUSPEND, Action.RESUME):
        decide = commands.add_parser(action.value, help=f"{action.value} a creative")
        add_prefix_argument(decide)
        decide.add_argument("crid")
        decide.add_argument("--by", default="operator")
        decide.add_argument("--note", default="")
        decide.add_argument("--wait", action="store_true")
    args = parser.parse_args()
    creatives = Creatives(Stage.of(args.prefix))
    match args.command:
        case "status":
            rows = [creatives.record(args.crid)] if args.crid else creatives.records()
            for row in rows:
                print(json.dumps(summary(row), default=str))
            return 0
        case "register":
            details = {"advertiser": args.advertiser, "adomain": args.adomain, "by": args.by}
            crid, execution = (
                creatives.register_file(args.file, **details)
                if args.file
                else creatives.register_url(args.url, **details)
            )
        case action:
            crid = args.crid
            execution = creatives.start(Decision(action=action, crid=crid, by=args.by, note=args.note))
    print(json.dumps({"crid": crid, "execution": execution}))
    if not args.wait:
        return 0
    ended = creatives.wait(execution)
    print(json.dumps({"execution": ended["status"], **summary(creatives.record(crid))}, default=str))
    return 0 if ended["status"] == "SUCCEEDED" else 1


if __name__ == "__main__":
    run(main)
