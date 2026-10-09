"""Runs the API under uvicorn and turns SIGTERM into a graceful drain."""

import logging
from types import FrameType

import uvicorn
from fastapi import FastAPI

from app.api import create_app
from app.config import Settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class DrainingServer(uvicorn.Server):
    """uvicorn's server, changed in one way: the application learns that it is stopping.

    When told to exit, uvicorn already does the draining: it stops listening,
    closes idle keep-alive connections, waits for every request it has accepted
    (running or queued for a slot) and then returns. What it does not do is tell
    the application first, so /readyz would go on answering "ready" meanwhile.
    """

    def __init__(self, app: FastAPI, host: str, port: int) -> None:
        # One process and one event loop per pod: the slots and the metrics live in
        # this process, and Kubernetes scales by adding pods. log_config=None leaves
        # logging to main(). The access log is off because the request metrics
        # carry that information, and one line per request would bury the rest.
        config = uvicorn.Config(app, host=host, port=port, log_config=None, access_log=False)
        super().__init__(config)
        self._app = app

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """Begin the drain. uvicorn installs this as the SIGTERM and SIGINT handler.

        The parent method also records the signal so that uvicorn can raise it
        again once it has shut down, which ends the process with "killed by
        SIGTERM". Leaving that out lets a drained process exit with status 0.
        It also means a second Ctrl-C does not cut the drain short.
        """
        self._app.state.draining = True
        self.should_exit = True


def main() -> None:
    # One format for uvicorn's log lines and the application's.
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    settings = Settings.from_env()
    logging.getLogger(__name__).info("starting with %s", settings)
    DrainingServer(create_app(settings), settings.host, settings.port).run()
