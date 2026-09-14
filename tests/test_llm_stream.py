import pytest

from backend import llm_stream
from backend.providers import ProviderError


class HeartbeatOnlyResponse:
    headers = {"content-type": "text/event-stream"}

    def iter_lines(self):
        yield ": keep-alive"
        yield ""


def test_transport_heartbeats_do_not_keep_an_empty_model_stream_alive(monkeypatch):
    times = iter((0.0, 2.0))
    monkeypatch.setattr(llm_stream.time, "monotonic", lambda: next(times))

    with pytest.raises(ProviderError, match="停止空白等待"):
        llm_stream.read_completion(HeartbeatOnlyResponse(), lambda *_: None, timeout_seconds=1)
