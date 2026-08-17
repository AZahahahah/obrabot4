from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
from typing import Any

import pytest

from obrabot4.github_worker import run_worker
from obrabot4.openai_responses import PROFILE_AUDIT_WORKER_PROTOCOL


TARGET_SHA = "a" * 40
RUN_ID = "123e4567-e89b-42d3-a456-426614174000"


def _credential_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    identity = tmp_path / "identity"
    known_hosts = tmp_path / "known_hosts"
    oidc = tmp_path / "oidc.jwt"
    identity.write_text("private-key-placeholder", encoding="utf-8")
    known_hosts.write_text("host ssh-ed25519 public-key", encoding="utf-8")
    oidc.write_text("signed.oidc.token", encoding="ascii")
    return identity, known_hosts, oidc


def _arguments(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    identity, known_hosts, oidc = _credential_files(tmp_path)
    values: dict[str, object] = {
        "host": "server.example.test",
        "user": "deploy",
        "identity_file": str(identity),
        "known_hosts_file": str(known_hosts),
        "oidc_token_file": str(oidc),
        "target_sha": TARGET_SHA,
        "operation": "audit",
        "run_id": RUN_ID,
        "concurrency": 4,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _install_fake_ssh(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "ssh"
    record = tmp_path / "ssh-record.json"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

record = Path(os.environ["FAKE_SSH_RECORD"])
oidc = sys.stdin.buffer.readline().decode("ascii").rstrip("\\n")
record.write_text(json.dumps({"argv": sys.argv[1:], "oidc": oidc}), encoding="utf-8")
mode = os.environ.get("FAKE_SSH_MODE", "request")
if mode == "malformed":
    sys.stdout.buffer.write(b"not-json\\n")
    sys.stdout.buffer.flush()
    raise SystemExit(0)
if mode == "idle":
    print(json.dumps({
        "type": "idle",
        "protocol": 1,
        "operation": "audit",
        "requested_run_id": "auto",
    }), flush=True)
    raise SystemExit(0)
print(json.dumps({
    "type": "started",
    "protocol": 1,
    "operation": "audit",
    "run_id": "123e4567-e89b-42d3-a456-426614174000",
}), flush=True)
print(json.dumps({
    "type": "openai_request",
    "protocol": 1,
    "request_id": "request-ssh-1",
    "timeout_seconds": 30,
    "payload": {"model": "gpt-5.6-luna", "store": False, "input": "private"},
}), flush=True)
response = json.loads(sys.stdin.buffer.readline().decode("utf-8"))
existing = json.loads(record.read_text(encoding="utf-8"))
existing["response"] = response
record.write_text(json.dumps(existing), encoding="utf-8")
status = os.environ.get("FAKE_SSH_STATUS", "completed")
print(json.dumps({
    "type": "completed",
    "protocol": 1,
    "operation": "audit",
    "run_id": "123e4567-e89b-42d3-a456-426614174000",
    "status": status,
    "processed_total": 1,
    "technical_failure_total": 0,
    "technical_error_counts": {},
}), flush=True)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return bin_dir, record


def _worker_environment(monkeypatch, bin_dir: Path, record: Path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-private-key-material")
    monkeypatch.setenv("FAKE_SSH_RECORD", str(record))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


def test_worker_uses_exact_ssh_command_and_correlates_response(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    bin_dir, record = _install_fake_ssh(tmp_path)
    _worker_environment(monkeypatch, bin_dir, record)
    arguments = _arguments(tmp_path)

    def executor(request: dict[str, Any], *, api_key: str) -> dict[str, Any]:
        assert request["payload"]["store"] is False
        assert api_key == "sk-test-private-key-material"
        return {
            "type": "openai_response",
            "protocol": PROFILE_AUDIT_WORKER_PROTOCOL,
            "request_id": request["request_id"],
            "response": {"id": "resp_from_test", "status": "completed"},
        }

    result = run_worker(arguments, request_executor=executor)

    assert result == 0
    captured = json.loads(record.read_text(encoding="utf-8"))
    assert captured == {
        "argv": [
            "-i",
            arguments.identity_file,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={arguments.known_hosts_file}",
            "deploy@server.example.test",
            f"profile-audit {TARGET_SHA} audit {RUN_ID}",
        ],
        "oidc": "signed.oidc.token",
        "response": {
            "type": "openai_response",
            "protocol": 1,
            "request_id": "request-ssh-1",
            "response": {"id": "resp_from_test", "status": "completed"},
        },
    }
    output = capsys.readouterr().out
    assert "private" not in output
    assert "resp_from_test" not in output
    assert "processed=1" in output


def test_worker_returns_zero_when_server_has_no_queued_work(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    bin_dir, record = _install_fake_ssh(tmp_path)
    _worker_environment(monkeypatch, bin_dir, record)
    monkeypatch.setenv("FAKE_SSH_MODE", "idle")

    assert run_worker(_arguments(tmp_path, run_id="auto")) == 0
    assert "No queued profile operation" in capsys.readouterr().out


def test_worker_returns_one_for_completed_audit_with_errors(
    tmp_path: Path, monkeypatch
) -> None:
    bin_dir, record = _install_fake_ssh(tmp_path)
    _worker_environment(monkeypatch, bin_dir, record)
    monkeypatch.setenv("FAKE_SSH_STATUS", "completed_with_errors")

    assert run_worker(
        _arguments(tmp_path),
        request_executor=lambda request, **_kwargs: {
            "type": "openai_response",
            "protocol": 1,
            "request_id": request["request_id"],
            "response": {},
        },
    ) == 1


def test_worker_stops_on_malformed_server_message(
    tmp_path: Path, monkeypatch
) -> None:
    bin_dir, record = _install_fake_ssh(tmp_path)
    _worker_environment(monkeypatch, bin_dir, record)
    monkeypatch.setenv("FAKE_SSH_MODE", "malformed")

    with pytest.raises(Exception, match="wire_message_invalid"):
        run_worker(_arguments(tmp_path))


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"target_sha": "abc"}, "invalid target SHA"),
        ({"run_id": "../secret"}, "invalid profile audit run id"),
        ({"host": "host;id"}, "invalid SSH destination"),
        ({"user": "deploy root"}, "invalid SSH destination"),
        ({"concurrency": 0}, "worker concurrency"),
        ({"concurrency": 17}, "worker concurrency"),
        ({"identity_file": "/missing/key"}, "credential file is missing"),
    ],
)
def test_worker_rejects_unsafe_arguments_before_starting_ssh(
    tmp_path: Path, monkeypatch, override: dict[str, object], expected: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-private-key-material")

    with pytest.raises(ValueError, match=expected):
        run_worker(_arguments(tmp_path, **override))


@pytest.mark.parametrize("api_key", ["", "not-a-key", "sk-short"])
def test_worker_rejects_missing_or_invalid_openai_key(
    tmp_path: Path, monkeypatch, api_key: str
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", api_key)

    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        run_worker(_arguments(tmp_path))


def test_worker_rejects_multiline_oidc_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-private-key-material")
    arguments = _arguments(tmp_path)
    Path(arguments.oidc_token_file).write_text("first\nsecond", encoding="ascii")

    with pytest.raises(ValueError, match="OIDC token"):
        run_worker(arguments)
