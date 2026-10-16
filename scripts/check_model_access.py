#!/usr/bin/env -S uv run python
"""Check, and with explicit consent enable, Amazon Bedrock access to the agent's models.

The agent's runtime role must never subscribe to AWS Marketplace products, so the first invocation of a third-party
model (which creates the subscription) has to happen with the deploying principal, before the agent is used.

    uv run python scripts/check_model_access.py                     # report only
    uv run python scripts/check_model_access.py --invoke            # also send one tiny Converse request
    uv run python scripts/check_model_access.py --accept-agreements # create missing model agreements

Anthropic models additionally need the account's one-time use-case form. Submit it in the Bedrock console, or with
``--use-case-form form.json`` (fields: companyName, companyWebsite, intendedUsers, industryOption, otherIndustryOption,
useCases), which calls ``PutUseCaseForModelAccess`` on your behalf.
"""

import argparse
import base64
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

REPO = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Availability:
    model_id: str
    agreement: str
    authorization: str
    entitlement: str
    region: str

    @property
    def ready(self) -> bool:
        return (self.agreement, self.authorization, self.entitlement, self.region) == (
            "AVAILABLE",
            "AUTHORIZED",
            "AVAILABLE",
            "AVAILABLE",
        )


def configured_models() -> list[str]:
    """The default and allow-listed models from cdk.json (``fast-channels.agent``)."""
    agent = json.loads((REPO / "cdk.json").read_text())["context"]["fast-channels"]["agent"]
    return list(dict.fromkeys([agent["model"], *agent.get("allowed_models", [])]))


def foundation_models(bedrock: object, model_or_profile: str) -> list[str]:
    """Foundation model ids behind an inference profile id (or the id itself)."""
    try:
        profile = bedrock.get_inference_profile(inferenceProfileIdentifier=model_or_profile)  # type: ignore[attr-defined]
    except ClientError:
        return [model_or_profile]
    return sorted({arn["modelArn"].rsplit("/", 1)[-1] for arn in profile["models"]})


def availability(bedrock: object, model_id: str) -> Availability:
    reply = bedrock.get_foundation_model_availability(modelId=model_id)  # type: ignore[attr-defined]
    return Availability(
        model_id=model_id,
        agreement=reply["agreementAvailability"]["status"],
        authorization=reply["authorizationStatus"],
        entitlement=reply["entitlementAvailability"],
        region=reply["regionAvailability"],
    )


def accept_agreement(bedrock: object, model_id: str) -> None:
    offers = bedrock.list_foundation_model_agreement_offers(modelId=model_id)["offers"]  # type: ignore[attr-defined]
    if not offers:
        raise SystemExit(f"{model_id}: no agreement offer available; enable it in the Bedrock console")
    bedrock.create_foundation_model_agreement(modelId=model_id, offerToken=offers[0]["offerToken"])  # type: ignore[attr-defined]
    print(f"{model_id}: agreement requested, waiting for it to become available …")
    for _ in range(30):
        if availability(bedrock, model_id).agreement == "AVAILABLE":
            return
        time.sleep(10)
    raise SystemExit(f"{model_id}: agreement still pending after 5 minutes; re-run later")


def smoke_test(model_or_profile: str, region: str) -> None:
    # Use SigV4 with the deploying principal (a Bedrock API key in AWS_BEARER_TOKEN_BEDROCK would bypass it).
    os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
    runtime = boto3.client("bedrock-runtime", region_name=region)
    started = time.perf_counter()
    reply = runtime.converse(
        modelId=model_or_profile,
        messages=[{"role": "user", "content": [{"text": "Reply with the single word OK."}]}],
        inferenceConfig={"maxTokens": 16},
    )
    text = reply["output"]["message"]["content"][0]["text"].strip()
    print(f"{model_or_profile}: invoked in {time.perf_counter() - started:.1f} s → {text!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", action="append", help="model or inference profile id (default: from cdk.json)")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION") or boto3.session.Session().region_name)
    parser.add_argument("--invoke", action="store_true", help="send one tiny Converse request per model")
    parser.add_argument("--accept-agreements", action="store_true", help="create missing model agreements")
    parser.add_argument("--use-case-form", type=Path, help="JSON file to submit as the Anthropic use-case form")
    args = parser.parse_args()

    bedrock = boto3.client("bedrock", region_name=args.region)
    if args.use_case_form:
        form = base64.b64encode(args.use_case_form.read_bytes())
        bedrock.put_use_case_for_model_access(formData=form)
        print("Anthropic use-case form submitted.")

    models = args.model or configured_models()
    problems = 0
    for model in models:
        for model_id in foundation_models(bedrock, model):
            status = availability(bedrock, model_id)
            if not status.ready and status.agreement != "AVAILABLE" and args.accept_agreements:
                accept_agreement(bedrock, model_id)
                status = availability(bedrock, model_id)
            mark = "ready" if status.ready else "NOT READY"
            print(
                f"{model} → {model_id}: {mark} (agreement {status.agreement}, authorization "
                f"{status.authorization}, entitlement {status.entitlement}, region {status.region})"
            )
            problems += not status.ready
        if args.invoke and not problems:
            smoke_test(model, args.region)

    if problems:
        print(
            "\nSome models are not ready. Re-run with --accept-agreements, submit the Anthropic use-case form, or "
            "choose another model in cdk.json (fast-channels.agent.model).",
            file=sys.stderr,
        )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
