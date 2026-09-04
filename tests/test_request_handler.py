from types import SimpleNamespace

from src.api import request_handler


def test_request_handler_applies_interval_between_get_and_post(monkeypatch):
    monotonic_values = iter([0.0, 0.0, 0.1, 0.1])
    sleeps = []
    responses = [SimpleNamespace(raise_for_status=lambda: None, json=lambda: {}),
                 SimpleNamespace(raise_for_status=lambda: None, json=lambda: {})]

    monkeypatch.setattr(request_handler.time, 'monotonic', lambda: next(monotonic_values))
    monkeypatch.setattr(request_handler.time, 'sleep', sleeps.append)
    monkeypatch.setattr(request_handler.config, 'API_REQUEST_INTERVAL_SECONDS', 0.5)
    monkeypatch.setattr(request_handler.requests, 'get', lambda *args, **kwargs: responses[0])
    monkeypatch.setattr(request_handler.requests, 'post', lambda *args, **kwargs: responses[1])
    request_handler._last_request_at = None

    request_handler.send_get('https://example.test')
    request_handler.send_post('https://example.test')

    assert sleeps == [0.4]