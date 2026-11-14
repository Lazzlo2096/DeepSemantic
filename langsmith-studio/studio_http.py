"""Browser access to the local Agent Server, including private-network preflights."""

import json
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware


# Agent Server 0.18 does not expose Starlette's allow_private_network setting
# in its CORS schema. A supported custom outer middleware handles preflights
# before the built-in middleware, using the same origin rules.
cors = json.loads(Path(__file__).with_name("langgraph.json").read_text(encoding="utf-8"))["http"]["cors"]
app = Starlette(middleware=[Middleware(CORSMiddleware, **cors, allow_private_network=True)])
