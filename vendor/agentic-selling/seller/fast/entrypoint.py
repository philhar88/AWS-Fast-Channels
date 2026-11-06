"""Start the Prebid Sales Agent (``serve``) or the FAST Channels bridge (``bridge``) in the seller's image.

The seller reads one ``DATABASE_URL`` and keys of its own (a Fernet key, the Flask secret, the signing keys' KEK). ECS
injects the database secret's fields (``DB_*``) and one random seed (``FAST_SECRET_SEED``), both from Secrets
Manager, so the values are composed and derived here and never stored in a task definition. Python 3.12, standard
library only: it runs before anything else in the image.
"""

import base64
import hashlib
import os
import sys
from urllib.parse import quote

COMMANDS = {
    "serve": ["/opt/venv/bin/python", "scripts/deploy/run_all_services.py"],
    "bridge": ["/opt/venv/bin/python", "/app/fast/bridge.py"],
}


def derived(seed: str, purpose: str) -> bytes:
    """A key for one ``purpose``, derived from the seed (each purpose gets its own)."""
    return hashlib.sha256(f"fast-channels:{purpose}:{seed}".encode()).digest()


def environment(env: dict[str, str]) -> dict[str, str]:
    """``env`` with the database URL and the seller's keys."""
    seed = env.pop("FAST_SECRET_SEED")
    user, password = quote(env["DB_USER"], safe=""), quote(env.pop("DB_PASSWORD"), safe="")
    host, port, name = env["DB_HOST"], env.get("DB_PORT", "5432"), env["DB_NAME"]
    ssl = env.get("DB_SSLMODE", "require")
    return {
        **env,
        "DATABASE_URL": f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode={ssl}",
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(derived(seed, "encryption")).decode(),
        "FLASK_SECRET_KEY": derived(seed, "flask").hex(),
        "SALESAGENT_SIGNING_KEK": derived(seed, "signing-kek").hex(),
        "ADCP_SIGNING_KEY_PASSPHRASE_ENV": "SALESAGENT_SIGNING_KEK",
    }


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command not in COMMANDS:
        sys.exit(f"usage: entrypoint.py [{'|'.join(COMMANDS)}]")
    os.chdir("/app")
    argv = COMMANDS[command]
    os.execve(argv[0], argv, environment(dict(os.environ)))  # noqa: S606 - becomes the seller, with no shell


if __name__ == "__main__":
    main()
