#!/usr/bin/env python
import time

from backend.db import Record, Session


with Session() as database:
    heartbeat = database.get(Record, "worker_heartbeat")
    healthy = bool(heartbeat and time.time() - heartbeat.data.get("at", 0) < 60)

raise SystemExit(0 if healthy else 1)
