"""Entry point: `python -m loadgen --url URL --rate N --seconds N`.

Prints the summary of the run to standard output, as one line of JSON.
Progress, and every failed request as it happens, go to standard error.
"""

import argparse
import asyncio
import json
import logging

from loadgen.load import run

parser = argparse.ArgumentParser(
    prog="python -m loadgen",
    description="Send requests to the service at a fixed rate and report how they went.",
)
parser.add_argument("--url", required=True, help="where to POST, for example http://host/v1/infer")
parser.add_argument("--rate", required=True, type=float, help="requests per second")
parser.add_argument("--seconds", required=True, type=float, help="how long to send them for")
parser.add_argument(
    "--connections",
    type=int,
    default=8,
    help="clients that keep a connection open; they send every other request (default: 8)",
)
parser.add_argument(
    "--max-idle",
    type=float,
    default=2.0,
    help="seconds after which an unused connection is not used again; has to be shorter than"
    " the server's keep-alive timeout (default: 2)",
)
parser.add_argument(
    "--request-timeout",
    type=float,
    default=10.0,
    help="seconds after which an unanswered request has failed (default: 10)",
)

arguments = parser.parse_args()
if arguments.connections < 1:
    # Every other request waits for one of these clients to take it.
    parser.error("--connections has to be at least 1")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
print(json.dumps(asyncio.run(run(**vars(arguments)))))
