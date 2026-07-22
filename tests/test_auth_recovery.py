import importlib.util
from pathlib import Path


MODULE_PATH = (
    Path(__file__).parents[1]
    / "custom_components"
    / "cloudedge"
    / "auth_recovery.py"
)
SPEC = importlib.util.spec_from_file_location("auth_recovery", MODULE_PATH)
AUTH_RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUTH_RECOVERY)


class _Client:
    def __init__(self, result=True):
        self.session_data = {"userToken": "rejected-token"}
        self.result = result
        self.calls = []

    def authenticate(self, *, force_refresh=False):
        self.calls.append((force_refresh, self.session_data))
        if self.result:
            self.session_data = {"userToken": "fresh-token"}
        return self.result


def test_invalid_session_forces_login_and_rotates_transport():
    client = _Client()
    events = []

    result = AUTH_RECOVERY.refresh_invalid_session(
        client,
        stop_transport=lambda: events.append("stop"),
        start_transport=lambda: events.append("start"),
    )

    assert result is True
    assert client.calls == [(True, None)]
    assert client.session_data == {"userToken": "fresh-token"}
    assert events == ["stop", "start"]


def test_failed_refresh_leaves_transport_stopped():
    client = _Client(result=False)
    events = []

    result = AUTH_RECOVERY.refresh_invalid_session(
        client,
        stop_transport=lambda: events.append("stop"),
        start_transport=lambda: events.append("start"),
    )

    assert result is False
    assert client.calls == [(True, None)]
    assert events == ["stop"]
