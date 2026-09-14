"""Production ASGI entry point used only by the container deployment."""

import os

from .app import app as local_app
from .production_boundary import ProductionOriginBoundary


public_origin = os.getenv("STORYLOOM_PUBLIC_ORIGIN", "")
if not public_origin:
    raise RuntimeError("STORYLOOM_PUBLIC_ORIGIN is required for the production server")

app = ProductionOriginBoundary(local_app, public_origin)
