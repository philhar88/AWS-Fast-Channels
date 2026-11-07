#!/usr/bin/env -S uv run python
"""Create the demo personas' viewers in the identity module's viewer pool (``modules.identity``).

    uv run python scripts/seed_personas.py --prefix fastm                     # the signed-in personas of the contract
    uv run python scripts/seed_personas.py --prefix fastm --persona dana --persona ben
    uv run python scripts/seed_personas.py --prefix fastm --delete            # remove them and their secret

The personas come from ``contracts/personas.json`` (``fast_channels.core.personas``: each entry with ``sign_in: user``;
the guest and kids profiles have no pool user), or from ``--persona``. Each becomes a pool user
``<id>@personas.example.com`` (or the entry's ``email``) with ``custom:persona=<id>`` and a permanent password. No email
is sent. The passwords are written to the Secrets Manager secret ``<prefix>/identity/demo-users`` as
``{"<persona>": {"email": ..., "password": ...}}``; a re-run keeps them. The script prints each persona's ``vid``
(``core.identity.vid.viewer_id``), the id the Pod Decision Service knows the viewer by; the raw ``sub`` is never
printed or stored.

Each persona of the contract also gets the consent its mode stands for in the identity table (``C#<vid>``,
``fast_channels.core.identity.consent``): FULL gave the video-privacy consent, PSEUDONYMOUS neither, CONTEXTUAL
opted out. Without a consent a signed-in viewer's breaks are SAFE. A ``--persona`` outside the contract gets none.

With the ``pds`` module, each persona of the contract that has an ad-load tier also gets its profile in the PDS table
(``VIEWER#<vid>``, ``fast_channels.functions.pds.profiles.put_profile``, ADR-0037). ``--delete`` removes the consent
and the profile.
"""

import argparse
import json
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from _stack import REPO, Stage, add_prefix_argument, run
from botocore.exceptions import ClientError

from fast_channels.core.identity import consent as consents
from fast_channels.core.identity.consent import ConsentChoice
from fast_channels.core.identity.names import (
    PERSONA_ATTRIBUTE,
    Parameter,
    demo_users_secret,
    parameter_name,
)
from fast_channels.core.identity.vid import viewer_id
from fast_channels.core.pds.models import Mode
from fast_channels.core.pds.names import table_name
from fast_channels.core.pds.viewers import profile_key
from fast_channels.core.personas import persona as contract_persona
from fast_channels.functions.pds.profiles import put_profile
from fast_channels.functions.pds.reservations import item_key

if TYPE_CHECKING:
    from mypy_boto3_cognito_idp import CognitoIdentityProviderClient
    from mypy_boto3_cognito_idp.type_defs import AttributeTypeTypeDef

CONTRACT: Final = REPO / "contracts" / "personas.json"
EMAIL_DOMAIN: Final = "personas.example.com"
SIGNED_IN: Final = "user"
"""The ``sign_in`` of a persona with a pool user (``fast_channels.core.personas.SignIn.USER``)."""
CHOICES: Final = {
    Mode.FULL: ConsentChoice(opt_out=False, vppa=True),
    Mode.PSEUDONYMOUS: ConsentChoice(opt_out=False),
    Mode.CONTEXTUAL: ConsentChoice(opt_out=True),
}
"""The consent a signed-in persona's mode stands for."""


def contract_personas(path: Path = CONTRACT) -> list[dict[str, str]]:
    """The personas of ``path`` with a pool user: ``{"id", "email"}`` each (the list, or its ``personas`` key)."""
    if not path.exists():
        return []
    document = json.loads(path.read_text())
    entries: list[dict[str, Any]] = document.get("personas", []) if isinstance(document, dict) else document
    return [
        {"id": str(entry["id"]), "email": str(entry.get("email") or f"{entry['id']}@{EMAIL_DOMAIN}")}
        for entry in entries
        if entry.get("sign_in") == SIGNED_IN
    ]


def password() -> str:
    """A password the pool's policy accepts (12+ characters with every class)."""
    return f"{secrets.token_urlsafe(18)}aA1!"


def _parameter(stage: Stage, which: Parameter) -> str:
    value: str = stage.session.client("ssm").get_parameter(Name=parameter_name(stage.prefix, which))["Parameter"][
        "Value"
    ]
    return value


def _secret(stage: Stage) -> dict[str, dict[str, str]]:
    try:
        raw = stage.session.client("secretsmanager").get_secret_value(SecretId=demo_users_secret(stage.prefix))
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return {}
        raise
    existing: dict[str, dict[str, str]] = json.loads(raw["SecretString"])
    return existing


def _write_secret(stage: Stage, users: dict[str, dict[str, str]]) -> None:
    client: Any = stage.session.client("secretsmanager")
    name, value = demo_users_secret(stage.prefix), json.dumps(users, sort_keys=True)
    try:
        client.put_secret_value(SecretId=name, SecretString=value)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        client.create_secret(
            Name=name,
            SecretString=value,
            Description="Demo viewers' sign-in (scripts/seed_personas.py)",
            Tags=[{"Key": "fast:app", "Value": stage.prefix}, {"Key": "Project", "Value": "fast-channels"}],
        )


def tier_of(persona: str) -> str | None:
    """The ad-load tier the contract gives ``persona``; ``None`` for one without a profile or not in the contract."""
    try:
        return contract_persona(persona).tier
    except ValueError:
        return None


def choice_of(persona: str) -> ConsentChoice | None:
    """The consent the contract's mode of a signed-in ``persona`` stands for; ``None`` for a guest, a kids profile or
    a persona outside the contract."""
    try:
        entry = contract_persona(persona)
    except ValueError:
        return None
    return CHOICES.get(entry.mode) if entry.sign_in == SIGNED_IN else None


def _consent(stage: Stage, persona: str, vid: str) -> str | None:
    """Record ``persona``'s consent (``C#<vid>``); the consent it gives, or ``None`` without one."""
    if (choice := choice_of(persona)) is None:
        return None
    row = consents.record(vid, choice, now_ms=time.time_ns() // 1_000_000)
    stage.session.resource("dynamodb").Table(_parameter(stage, Parameter.TABLE)).put_item(Item=row.item())
    return consents.consent_of(row, now=datetime.now(UTC)).value


def _missing_table(error: ClientError) -> bool:
    return error.response.get("Error", {}).get("Code") == "ResourceNotFoundException"


def _profile(stage: Stage, persona: str, vid: str) -> str | None:
    """Write ``persona``'s PDS profile (``VIEWER#<vid>``); its tier, or ``None`` without a tier or a PDS table."""
    if (tier := tier_of(persona)) is None:
        return None
    try:
        put_profile(stage.session.client("dynamodb"), table_name(stage.prefix), vid, persona)
    except ClientError as error:
        if _missing_table(error):  # the stage runs without the pds module
            return None
        raise
    return tier


def _delete_profile(stage: Stage, vid: str) -> None:
    try:
        stage.session.client("dynamodb").delete_item(TableName=table_name(stage.prefix), Key=item_key(profile_key(vid)))
    except ClientError as error:
        if not _missing_table(error):
            raise


def _user(cognito: CognitoIdentityProviderClient, pool: str, persona: str, email: str, secret: str) -> str:
    """Create (or update) the persona's user; its ``sub``."""
    attributes: list[AttributeTypeTypeDef] = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
        {"Name": f"custom:{PERSONA_ATTRIBUTE}", "Value": persona},
    ]
    try:
        cognito.admin_create_user(UserPoolId=pool, Username=email, UserAttributes=attributes, MessageAction="SUPPRESS")
    except cognito.exceptions.UsernameExistsException:
        cognito.admin_update_user_attributes(UserPoolId=pool, Username=email, UserAttributes=attributes[2:])
    cognito.admin_set_user_password(UserPoolId=pool, Username=email, Password=secret, Permanent=True)
    user = cognito.admin_get_user(UserPoolId=pool, Username=email)
    return next(a["Value"] for a in user["UserAttributes"] if a["Name"] == "sub")


def seed(stage: Stage, personas: list[dict[str, str]]) -> list[dict[str, str]]:
    """Create ``personas`` in the stage's pool, with their consent and PDS profiles; each one's
    ``{persona, email, vid}``, with ``consent`` and ``tier`` where they were written."""
    cognito: CognitoIdentityProviderClient = stage.session.client("cognito-idp")
    pool, issuer = _parameter(stage, Parameter.USER_POOL_ID), _parameter(stage, Parameter.ISSUER)
    key = _parameter(stage, Parameter.VID_KEY)
    kms = stage.session.client("kms")
    users = _secret(stage)
    seeded = []
    for entry in personas:
        persona, email = entry["id"], entry["email"]
        secret = users.get(persona, {}).get("password") or password()
        sub = _user(cognito, pool, persona, email, secret)
        vid = viewer_id(kms, key_id=key, issuer=issuer, sub=sub)
        consent = _consent(stage, persona, vid)
        tier = _profile(stage, persona, vid)
        users[persona] = {"email": email, "password": secret}
        seeded.append(
            {"persona": persona, "email": email, "vid": vid}
            | ({"consent": consent} if consent else {})
            | ({"tier": tier} if tier else {})
        )
    _write_secret(stage, users)
    return seeded


def delete(stage: Stage, personas: list[dict[str, str]]) -> list[str]:
    """Delete ``personas``' users, their consent and PDS profiles (and the secret once none is left); the deleted
    personas."""
    cognito: CognitoIdentityProviderClient = stage.session.client("cognito-idp")
    pool, issuer = _parameter(stage, Parameter.USER_POOL_ID), _parameter(stage, Parameter.ISSUER)
    key, kms = _parameter(stage, Parameter.VID_KEY), stage.session.client("kms")
    identity = stage.session.resource("dynamodb").Table(_parameter(stage, Parameter.TABLE))
    users = _secret(stage)
    deleted = []
    for entry in personas:
        try:
            user = cognito.admin_get_user(UserPoolId=pool, Username=entry["email"])
            sub = next(a["Value"] for a in user["UserAttributes"] if a["Name"] == "sub")
            vid = viewer_id(kms, key_id=key, issuer=issuer, sub=sub)
            identity.delete_item(Key={"pk": consents.key(vid)})
            _delete_profile(stage, vid)
            cognito.admin_delete_user(UserPoolId=pool, Username=entry["email"])
            deleted.append(entry["id"])
        except cognito.exceptions.UserNotFoundException:
            pass
        users.pop(entry["id"], None)
    client: Any = stage.session.client("secretsmanager")
    if users:
        _write_secret(stage, users)
    elif _secret(stage) or deleted:
        try:
            client.delete_secret(SecretId=demo_users_secret(stage.prefix), ForceDeleteWithoutRecovery=True)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_prefix_argument(parser)
    parser.add_argument("--persona", action="append", help="a persona id (default: the contract's signed-in personas)")
    parser.add_argument("--delete", action="store_true", help="delete the personas' users instead")
    args = parser.parse_args()
    stage = Stage.of(args.prefix)
    personas = (
        [{"id": p, "email": f"{p}@{EMAIL_DOMAIN}"} for p in args.persona] if args.persona else contract_personas()
    )
    if not personas:
        print(f"no personas: pass --persona, or add {CONTRACT.relative_to(REPO)}", file=sys.stderr)
        return 1
    if args.delete:
        print(json.dumps({"deleted": delete(stage, personas)}))
        return 0
    for row in seed(stage, personas):
        print(json.dumps(row))
    return 0


if __name__ == "__main__":
    run(main)
