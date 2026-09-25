import threading

import bridge


class _StubServer:
    def __init__(self):
        self.stopped = threading.Event()

    def shutdown(self):
        self.stopped.set()


def test_intentional_shutdown_stops_supervision_and_bridge(monkeypatch):
    server = _StubServer()
    monkeypatch.setattr(bridge, "log", lambda _message: None)
    bridge._bridge_shutdown_scheduled.clear()
    bridge._tts_manager_stop.clear()
    try:
        thread = bridge._schedule_bridge_shutdown(server, delay=0)
        assert thread is not None
        assert bridge._tts_manager_stop.is_set()
        thread.join(timeout=1)
        assert server.stopped.is_set()
        assert bridge._schedule_bridge_shutdown(server, delay=0) is None
    finally:
        bridge._bridge_shutdown_scheduled.clear()
        bridge._tts_manager_stop.clear()
