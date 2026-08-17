from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, BinaryIO


MAX_WIRE_MESSAGE_BYTES = 1024 * 1024


class ProtocolError(RuntimeError):
    """A sanitized wire-protocol failure safe to expose in runner logs."""


def encode_wire_message(message: Mapping[str, Any]) -> bytes:
    if not isinstance(message, Mapping):
        raise ProtocolError("wire_message_invalid")
    try:
        encoded = json.dumps(
            dict(message),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as error:
        raise ProtocolError("wire_message_invalid") from error
    if len(encoded) > MAX_WIRE_MESSAGE_BYTES:
        raise ProtocolError("wire_message_too_large")
    return encoded


def read_wire_message(stream: BinaryIO) -> dict[str, Any] | None:
    line = stream.readline(MAX_WIRE_MESSAGE_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_WIRE_MESSAGE_BYTES:
        raise ProtocolError("wire_message_too_large")
    if not line.endswith(b"\n"):
        raise ProtocolError("wire_message_invalid")
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("wire_message_invalid") from error
    if not isinstance(message, dict):
        raise ProtocolError("wire_message_invalid")
    return message
