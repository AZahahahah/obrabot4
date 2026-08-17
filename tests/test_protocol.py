from __future__ import annotations

from io import BytesIO
import math

import pytest

from obrabot4.protocol import (
    MAX_WIRE_MESSAGE_BYTES,
    ProtocolError,
    encode_wire_message,
    read_wire_message,
)


def test_round_trip_preserves_unicode_object() -> None:
    message = {
        "type": "openai_request",
        "request_id": "request-1",
        "payload": {"input": "Иваново"},
    }

    encoded = encode_wire_message(message)

    assert encoded.endswith(b"\n")
    assert read_wire_message(BytesIO(encoded)) == message


def test_encode_rejects_message_larger_than_one_mebibyte() -> None:
    with pytest.raises(ProtocolError, match="^wire_message_too_large$"):
        encode_wire_message({"payload": "x" * MAX_WIRE_MESSAGE_BYTES})


@pytest.mark.parametrize(
    "message",
    [
        ["not", "an", "object"],
        {"value": math.nan},
        {"value": object()},
    ],
)
def test_encode_rejects_non_object_or_non_json_message(message: object) -> None:
    with pytest.raises(ProtocolError, match="^wire_message_invalid$"):
        encode_wire_message(message)


@pytest.mark.parametrize(
    "wire",
    [
        b'[{"not":"an object"}]\n',
        b'{"unterminated":true',
        b"\xff\n",
        b'{"valid":true}',
    ],
)
def test_read_rejects_malformed_or_unframed_messages(wire: bytes) -> None:
    with pytest.raises(ProtocolError, match="^wire_message_invalid$"):
        read_wire_message(BytesIO(wire))


def test_read_rejects_overlong_line_without_consuming_unbounded_data() -> None:
    wire = b"{" + b"x" * MAX_WIRE_MESSAGE_BYTES + b"}\n"

    with pytest.raises(ProtocolError, match="^wire_message_too_large$"):
        read_wire_message(BytesIO(wire))


def test_read_returns_none_at_clean_eof() -> None:
    assert read_wire_message(BytesIO()) is None
