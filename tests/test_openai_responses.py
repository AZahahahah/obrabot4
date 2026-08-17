from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import urllib.error
from collections.abc import Iterator
from typing import Any

import pytest

from obrabot4 import openai_responses
from obrabot4.openai_responses import (
    PROFILE_AUDIT_WORKER_PROTOCOL,
    ResponsesAPIError,
    default_responses_transport,
    execute_openai_wire_request,
)


class _Handler(BaseHTTPRequestHandler):
    response_status = 200
    response_document: dict[str, Any] | list[Any] | bytes = {
        "id": "resp_test",
        "status": "completed",
        "output": [],
    }
    received: dict[str, Any] = {}

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).received = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "content_type": self.headers.get("Content-Type"),
            "user_agent": self.headers.get("User-Agent"),
            "body": json.loads(body.decode("utf-8")),
        }
        document = type(self).response_document
        encoded = document if isinstance(document, bytes) else json.dumps(document).encode()
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *args: object) -> None:
        return


@contextmanager
def _server(
    *, status: int = 200, document: dict[str, Any] | list[Any] | bytes
) -> Iterator[str]:
    handler = type(
        "ScenarioHandler",
        (_Handler,),
        {"response_status": status, "response_document": document, "received": {}},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1/responses"
        _Handler.received = handler.received
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_transport_posts_private_request_and_returns_object(monkeypatch) -> None:
    response = {"id": "resp_success", "status": "completed", "output": []}
    with _server(document=response) as url:
        monkeypatch.setattr(openai_responses, "OPENAI_RESPONSES_URL", url)

        result = default_responses_transport(
            {"model": "gpt-5.6-luna", "store": False, "input": "проверка"},
            "sk-test-private-key-material",
            5.0,
        )

    assert result == response
    assert _Handler.received == {
        "path": "/v1/responses",
        "authorization": "Bearer sk-test-private-key-material",
        "content_type": "application/json",
        "user_agent": "obrabot4/1.0",
        "body": {
            "model": "gpt-5.6-luna",
            "store": False,
            "input": "проверка",
        },
    }


@pytest.mark.parametrize("store", [None, True, "false", 0])
def test_transport_refuses_requests_that_may_be_stored(store: object) -> None:
    payload = {"model": "gpt-5.6-luna", "input": "profile"}
    if store is not None:
        payload["store"] = store

    with pytest.raises(ResponsesAPIError) as caught:
        default_responses_transport(payload, "sk-test-private-key-material", 5.0)

    assert caught.value.code == "openai_store_must_be_false"
    assert caught.value.retryable is False
    assert caught.value.status_code is None


@pytest.mark.parametrize(
    ("code", "expected_retryable"),
    [
        ("rate_limit_exceeded", True),
        ("insufficient_quota", False),
    ],
)
def test_transport_classifies_http_429_without_exposing_error_message(
    monkeypatch, code: str, expected_retryable: bool
) -> None:
    document = {
        "error": {
            "message": "sensitive upstream detail",
            "type": "requests",
            "code": code,
        }
    }
    with _server(status=429, document=document) as url:
        monkeypatch.setattr(openai_responses, "OPENAI_RESPONSES_URL", url)
        with pytest.raises(ResponsesAPIError) as caught:
            default_responses_transport(
                {"model": "gpt-5.6-luna", "store": False, "input": "profile"},
                "sk-test-private-key-material",
                5.0,
            )

    assert str(caught.value) == code
    assert "sensitive" not in repr(caught.value)
    assert caught.value.status_code == 429
    assert caught.value.retryable is expected_retryable
    assert caught.value.error_type == "requests"


def test_transport_rejects_non_object_openai_response(monkeypatch) -> None:
    with _server(document=["not", "an", "object"]) as url:
        monkeypatch.setattr(openai_responses, "OPENAI_RESPONSES_URL", url)
        with pytest.raises(ResponsesAPIError) as caught:
            default_responses_transport(
                {"model": "gpt-5.6-luna", "store": False, "input": "profile"},
                "sk-test-private-key-material",
                5.0,
            )

    assert caught.value.code == "invalid_openai_response"
    assert caught.value.retryable is True


def test_transport_normalizes_socket_timeout(monkeypatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError(socket.timeout("private detail"))

    monkeypatch.setattr(openai_responses.urllib.request, "urlopen", fail)

    with pytest.raises(ResponsesAPIError) as caught:
        default_responses_transport(
            {"model": "gpt-5.6-luna", "store": False, "input": "profile"},
            "sk-test-private-key-material",
            5.0,
        )

    assert str(caught.value) == "openai_timeout"
    assert caught.value.retryable is True


def test_execute_wire_request_returns_correlated_response() -> None:
    expected = {"id": "resp_worker", "status": "completed", "output": []}

    def transport(payload: dict[str, Any], api_key: str, timeout: float) -> dict[str, Any]:
        assert payload == {"model": "gpt-5.6-luna", "store": False}
        assert api_key == "sk-test-private-key-material"
        assert timeout == 30.0
        return expected

    result = execute_openai_wire_request(
        {
            "type": "openai_request",
            "protocol": PROFILE_AUDIT_WORKER_PROTOCOL,
            "request_id": "request-123",
            "timeout_seconds": 30,
            "payload": {"model": "gpt-5.6-luna", "store": False},
        },
        api_key="sk-test-private-key-material",
        transport=transport,
    )

    assert result == {
        "type": "openai_response",
        "protocol": PROFILE_AUDIT_WORKER_PROTOCOL,
        "request_id": "request-123",
        "response": expected,
    }


def test_execute_wire_request_returns_only_sanitized_error_fields() -> None:
    def transport(
        _payload: dict[str, Any], _api_key: str, _timeout: float
    ) -> dict[str, Any]:
        raise ResponsesAPIError(
            "rate_limit_exceeded",
            status_code=429,
            retryable=True,
            error_type="requests",
        )

    result = execute_openai_wire_request(
        {
            "type": "openai_request",
            "protocol": PROFILE_AUDIT_WORKER_PROTOCOL,
            "request_id": "request-429",
            "timeout_seconds": 30,
            "payload": {"model": "gpt-5.6-luna", "store": False},
        },
        api_key="sk-test-private-key-material",
        transport=transport,
    )

    assert result == {
        "type": "openai_error",
        "protocol": PROFILE_AUDIT_WORKER_PROTOCOL,
        "request_id": "request-429",
        "code": "rate_limit_exceeded",
        "status_code": 429,
        "retryable": True,
        "error_type": "requests",
    }


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("type", "other", "wire_request_invalid"),
        ("protocol", 2, "wire_request_invalid"),
        ("request_id", "bad request id", "wire_request_id_invalid"),
        ("payload", [], "wire_payload_invalid"),
        ("timeout_seconds", 2.9, "wire_timeout_invalid"),
        ("timeout_seconds", 120.1, "wire_timeout_invalid"),
        ("timeout_seconds", True, "wire_timeout_invalid"),
    ],
)
def test_execute_wire_request_rejects_invalid_envelope(
    field: str, value: object, error_code: str
) -> None:
    request: dict[str, Any] = {
        "type": "openai_request",
        "protocol": PROFILE_AUDIT_WORKER_PROTOCOL,
        "request_id": "request-valid",
        "timeout_seconds": 30,
        "payload": {"model": "gpt-5.6-luna", "store": False},
    }
    request[field] = value

    with pytest.raises(Exception, match=f"^{error_code}$"):
        execute_openai_wire_request(
            request,
            api_key="sk-test-private-key-material",
            transport=lambda *_args: {},
        )
