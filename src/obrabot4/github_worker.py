from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import Any

from obrabot4.openai_responses import (
    PROFILE_AUDIT_WORKER_PROTOCOL,
    execute_openai_wire_request,
)
from obrabot4.protocol import ProtocolError, encode_wire_message, read_wire_message


_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(
    r"^(?:auto|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_SSH_VALUE = re.compile(r"^[A-Za-z0-9._:@-]{1,255}$")
RequestExecutor = Callable[..., dict[str, Any]]


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--identity-file", required=True)
    parser.add_argument("--known-hosts-file", required=True)
    parser.add_argument("--oidc-token-file", required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--operation", choices=("audit", "repair"), required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    return parser.parse_args()


def _validate_arguments(arguments: argparse.Namespace) -> None:
    if not _SHA.fullmatch(arguments.target_sha):
        raise ValueError("invalid target SHA")
    if not _RUN_ID.fullmatch(arguments.run_id):
        raise ValueError("invalid profile audit run id")
    if not _SSH_VALUE.fullmatch(arguments.host) or not _SSH_VALUE.fullmatch(
        arguments.user
    ):
        raise ValueError("invalid SSH destination")
    if not 1 <= arguments.concurrency <= 16:
        raise ValueError("worker concurrency must be between 1 and 16")
    for value in (
        arguments.identity_file,
        arguments.known_hosts_file,
        arguments.oidc_token_file,
    ):
        if not Path(value).is_file():
            raise ValueError("worker credential file is missing")


def _protocol_error_response(request: dict[str, Any]) -> dict[str, Any]:
    request_id = request.get("request_id")
    return {
        "type": "openai_error",
        "protocol": PROFILE_AUDIT_WORKER_PROTOCOL,
        "request_id": request_id if isinstance(request_id, str) else "invalid",
        "code": "github_worker_protocol_error",
        "status_code": None,
        "retryable": False,
        "error_type": None,
    }


def _ssh_command(arguments: argparse.Namespace) -> list[str]:
    return [
        "ssh",
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
        f"{arguments.user}@{arguments.host}",
        (
            f"profile-audit {arguments.target_sha} "
            f"{arguments.operation} {arguments.run_id}"
        ),
    ]


def run_worker(
    arguments: argparse.Namespace,
    *,
    request_executor: RequestExecutor = execute_openai_wire_request,
) -> int:
    _validate_arguments(arguments)
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not 20 <= len(api_key) <= 4096 or not api_key.startswith("sk-"):
        raise ValueError("OPENAI_API_KEY is missing or invalid")
    oidc_token = Path(arguments.oidc_token_file).read_text(encoding="ascii").strip()
    if (
        not oidc_token
        or len(oidc_token) > 16_384
        or "\n" in oidc_token
        or "\r" in oidc_token
    ):
        raise ValueError("GitHub OIDC token is missing or invalid")

    process = subprocess.Popen(
        _ssh_command(arguments),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    write_lock = threading.Lock()
    futures: list[Future[dict[str, Any]]] = []
    final_message: dict[str, Any] | None = None

    def write_response(future: Future[dict[str, Any]], request: dict[str, Any]) -> None:
        try:
            response = future.result()
        except Exception:
            response = _protocol_error_response(request)
        with write_lock:
            try:
                process.stdin.write(encode_wire_message(response))
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                return

    try:
        process.stdin.write(oidc_token.encode("ascii") + b"\n")
        process.stdin.flush()
        oidc_token = ""
        with ThreadPoolExecutor(
            max_workers=arguments.concurrency,
            thread_name_prefix="openai-worker",
        ) as pool:
            while True:
                message = read_wire_message(process.stdout)
                if message is None:
                    break
                if message.get("type") == "openai_request":
                    future = pool.submit(
                        request_executor,
                        message,
                        api_key=api_key,
                    )
                    futures.append(future)
                    future.add_done_callback(
                        lambda done, request=message: write_response(done, request)
                    )
                    continue
                if message.get("protocol") != PROFILE_AUDIT_WORKER_PROTOCOL:
                    raise ProtocolError("wire_protocol_invalid")
                if message.get("type") == "started":
                    print(
                        "Profile worker started: "
                        f"operation={message.get('operation', '')}, "
                        f"run={message.get('run_id', '')}.",
                        flush=True,
                    )
                    continue
                if message.get("type") in {"idle", "completed"}:
                    final_message = message
                    break
                raise ProtocolError("wire_message_type_invalid")
            for future in futures:
                future.result()
        try:
            process.stdin.close()
        except OSError:
            pass
        return_code = process.wait(timeout=30)
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        raise
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        process.stdout.close()
        api_key = ""

    if return_code != 0:
        raise RuntimeError(f"remote profile audit exited with status {return_code}")
    if final_message is None:
        raise RuntimeError("remote profile audit ended without a final status")
    if final_message.get("type") == "idle":
        print("No queued profile operation requires processing.")
        return 0
    if final_message.get("operation") == "repair":
        print(
            "Profile repair finished: "
            f"status={final_message.get('status')}, "
            f"applied={final_message.get('applied_total')}, "
            f"pending={final_message.get('pending_total')}."
        )
        return 0
    technical_codes = final_message.get("technical_error_counts")
    if not isinstance(technical_codes, dict):
        technical_codes = {}
    code_summary = ",".join(
        f"{code}:{count}"
        for code, count in sorted(technical_codes.items())
        if isinstance(code, str)
        and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,99}", code)
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count > 0
    )
    print(
        "Profile audit finished: "
        f"status={final_message.get('status')}, "
        f"processed={final_message.get('processed_total')}, "
        f"technical_failures={final_message.get('technical_failure_total')}, "
        f"technical_codes={code_summary or 'none'}."
    )
    return 0 if final_message.get("status") == "completed" else 1


def main() -> int:
    try:
        return run_worker(_arguments())
    except Exception as error:
        print(f"Profile worker failed: {type(error).__name__}.", file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
