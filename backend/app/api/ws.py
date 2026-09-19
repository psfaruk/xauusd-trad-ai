"""WebSocket hub /ws — SPEC 7.2. Implemented in Phase 2 (bar_open/bar_update/
bar_close, tick, signal events; heartbeat 15s).

Phase 0 keeps this as an unmounted stub.
"""

from fastapi import APIRouter

router = APIRouter()

# TODO(phase-2): WS endpoint with token query param auth + subscribe protocol.
