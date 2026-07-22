"""Session recovery helpers shared by the CloudEdge coordinator."""

from __future__ import annotations

from typing import Any, Callable


def refresh_invalid_session(
    client: Any,
    *,
    stop_transport: Callable[[], None],
    start_transport: Callable[[], None],
) -> bool:
    """Replace a rejected cached session and rotate dependent transports.

    MQTT credentials are derived from the authenticated session. The old
    listener therefore has to stop before login and can only restart after a
    forced refresh succeeds.
    """
    stop_transport()
    client.session_data = None

    if not client.authenticate(force_refresh=True):
        return False

    start_transport()
    return True
