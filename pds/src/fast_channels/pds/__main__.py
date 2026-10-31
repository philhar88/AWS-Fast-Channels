"""``python -m fast_channels.pds``: uvicorn serving the sidecar (``server.app``) on ``127.0.0.1:8090``.

One process with uvloop and httptools, its own lifespan (the loads before it listens), no access log (every request
already logs its decision record), and uvicorn's own lines in Powertools' JSON. ``KEEP_ALIVE_S`` keeps an idle
connection open longer than Prebid Server's HTTP client does (Vert.x closes a pooled connection after 60 s idle), so
the client always closes first: with the server closing first, a request sent on a connection the server is closing
fails, and the module falls back (uvicorn's default is 5 s).
"""

import logging
from typing import Final

import uvicorn
from aws_lambda_powertools.logging.utils import copy_config_to_registered_loggers

from fast_channels.core.pds.names import PORT
from fast_channels.pds.server import app, logger

HOST: Final = "127.0.0.1"
"""Prebid Server reaches the sidecar on the task's loopback: nothing outside the task can."""
KEEP_ALIVE_S: Final = 120


def main(port: int = PORT) -> None:
    """Serve until SIGTERM (ECS stops Prebid Server's container first: it depends on this one)."""
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name)
    copy_config_to_registered_loggers(source_logger=logger, include={"uvicorn", "uvicorn.error"})
    uvicorn.run(
        app,
        host=HOST,
        port=port,
        loop="uvloop",
        http="httptools",
        lifespan="on",
        access_log=False,
        log_config=None,
        timeout_keep_alive=KEEP_ALIVE_S,
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    main()
