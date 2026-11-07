"""The envelope's ledger record: each deploy that changes the envelope appends its version (chain ``agents/envelope``).

A CDK trigger runs this function when it changes, which an envelope change does (the envelope is in its
environment). The entry holds the version and the whole envelope, so the ledger says which envelope every agent
action of the chains ``agents/<role>`` was decided under (each of those names the version).
"""

import json
import os
from typing import TYPE_CHECKING, Any

from aws_lambda_powertools import Logger

from fast_channels.core.agents.envelope import ENVELOPE_CHAIN, Envelope
from fast_channels.core.ledger import LedgerKind
from fast_channels.functions.ops.ledger import append, entry

if TYPE_CHECKING:
    from aws_lambda_powertools.utilities.typing import LambdaContext

logger = Logger()

PRINCIPAL = "deploy"
"""Who applies an envelope: the deployment (the owner signs envelope changes; a KMS signature is a follow-up)."""


@logger.inject_lambda_context(clear_state=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    """Append ``envelope.apply`` with the deployed envelope's version."""
    envelope = Envelope.model_validate_json(os.environ["ENVELOPE"])
    record = append(
        entry(
            ENVELOPE_CHAIN,
            LedgerKind.ENVELOPE,
            principal=PRINCIPAL,
            action="envelope.apply",
            subject=os.environ.get("FAST_PREFIX", "-"),
            correlation_id=context.aws_request_id,
            detail={
                "version": envelope.version,
                "agents": [role.value for role in envelope.enabled()],
                "envelope": json.loads(envelope.document),
            },
        )
    )
    logger.info("envelope applied", extra={"version": envelope.version, "record_id": record.record_id})
    return {"version": envelope.version, "record_id": record.record_id}
