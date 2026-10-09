"""Runs the API under uvicorn and turns SIGTERM into a graceful drain."""

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from types import FrameType

import uvicorn
from fastapi import FastAPI

from app.api import create_app
from app.config import Settings

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

logger = logging.getLogger(__name__)


class DrainingServer(uvicorn.Server):
    """uvicorn's server, with a drain that loses no request of a well-behaved client.

    What happens on SIGTERM, in order:

    1. The application is marked as draining: /readyz answers 503, and every
       response from now on carries `Connection: close`.
    2. The server stops listening. In a cluster nobody is trying to connect
       by then: the chart's preStop delay has given every node the time to
       stop sending new connections to this pod.
    3. The lame duck, one keep-alive timeout long. Connections that are open
       stay open. One that carries a request is closed after the response,
       which has told the client so. One that carries none is closed when it
       has been idle for the keep-alive timeout, as it would be on any other
       day.
    4. uvicorn's own shutdown: it closes the connections that are still idle,
       waits for every request that is running or queued for a slot, and
       returns.

    Step 3 is what uvicorn alone does not do. It closes idle connections the
    moment it is told to exit, and a client that is just then sending its next
    request on one of them loses that request. After a lame duck of one
    keep-alive timeout there is no such client left: every connection has
    either been told to close or has been idle for longer than a client may
    count on it.
    """

    def __init__(
        self,
        app: FastAPI,
        host: str,
        port: int,
        keep_alive_seconds: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        # One process and one event loop per pod: the slots and the metrics live in
        # this process, and Kubernetes scales by adding pods. log_config=None leaves
        # logging to main(). The access log is off because the request metrics
        # carry that information, and one line per request would bury the rest.
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            timeout_keep_alive=keep_alive_seconds,
            log_config=None,
            access_log=False,
        )
        super().__init__(config)
        self._app = app
        self._keep_alive_seconds = keep_alive_seconds
        # How the lame duck passes its time. Tests replace it so they decide when it ends.
        self._sleep = sleep

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """Begin the drain. uvicorn installs this as the SIGTERM and SIGINT handler.

        The parent method also records the signal so that uvicorn can raise it
        again once it has shut down, which ends the process with "killed by
        SIGTERM". Leaving that out lets a drained process exit with status 0.
        It also means a second Ctrl-C does not cut the drain short.
        """
        self._app.state.draining = True
        self.should_exit = True

    async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
        """Steps 2 to 4 of the drain. uvicorn calls this once it has been told to exit."""
        for listener in self.servers:
            listener.close()
        logger.info(
            "draining: not listening any more, open connections have %d seconds to finish",
            self._keep_alive_seconds,
        )
        await self._sleep(self._keep_alive_seconds)
        await super().shutdown(sockets=sockets)


def main() -> None:
    # One format for uvicorn's log lines and the application's.
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    settings = Settings.from_env()
    logger.info("starting with %s", settings)
    app = create_app(settings)
    DrainingServer(app, settings.host, settings.port, settings.keep_alive_seconds).run()
