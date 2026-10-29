"""Which DynamoDB Local containers of the test fixture a killed session left behind (``conftest.dynamodb_endpoint``)."""

from datetime import datetime, timedelta
from typing import Final

LABEL: Final = "fast-channels.test=dynamodb-local"
"""Every container the fixture starts carries this label."""
STALE: Final = timedelta(hours=2)
"""No test session runs this long: an older labelled container is a leftover; a younger one may be another
worktree's run in progress."""


def stale(listing: str, now: datetime) -> list[str]:
    """The ids of the containers in ``docker ps --format '{{.ID}} {{.CreatedAt}}'`` output created before
    ``now - STALE``."""
    old = []
    for line in listing.splitlines():
        container, created = line.split(" ", 1)
        if datetime.strptime(created.rsplit(" ", 1)[0], "%Y-%m-%d %H:%M:%S %z") < now - STALE:
            old.append(container)
    return old
