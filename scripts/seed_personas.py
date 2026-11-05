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
"""

import argparse
import json
import secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from _stack import REPO, Stage, add_prefix_argument, run
from botocore.exceptions import ClientError

from fast_channels.core.identity.names import (
    PERSONA_ATTRIBUTE,
    Parameter,
    demo_users_secret,
    parameter_name,
)
from fast_channels.core.identity.vid import viewer_id

if TYPE_CHECKING:
    from mypy_boto3_cognito_idp import CognitoIdentityProviderClient
    from mypy_boto3_cognito_idp.type_defs import AttributeTypeTypeDef

CONTRACT: Final = REPO / "contracts" / "personas.json"
EMAIL_DOMAIN: Final = "personas.example.com"
SIGNED_IN: Final = "user"
"""The ``sign_in`` of a persona with a pool user (``fast_channels.core.personas.SignIn.USER``)."""


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
    """Create ``personas`` in the stage's pool; each one's ``{persona, email, vid}``."""
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
        # B2 adds the persona's PDS profile here (fast_channels.functions.pds.profiles.put_profile).
        users[persona] = {"email": email, "password": secret}
        seeded.append({"persona": persona, "email": email, "vid": vid})
    _write_secret(stage, users)
    return seeded


def delete(stage: Stage, personas: list[dict[str, str]]) -> list[str]:
    """Delete ``personas``' users (and the secret once none is left); the deleted personas."""
    cognito: CognitoIdentityProviderClient = stage.session.client("cognito-idp")
    pool = _parameter(stage, Parameter.USER_POOL_ID)
    users = _secret(stage)
    deleted = []
    for entry in personas:
        try:
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
