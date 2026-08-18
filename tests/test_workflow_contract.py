from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
AUDIT_WORKFLOW = ROOT / ".github" / "workflows" / "profile-audit.yml"
AUDIT_RUNNER = ROOT / "scripts" / "run-profile-audit-worker.sh"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy-production.yml"
DEPLOY_RUNNER = ROOT / "scripts" / "deploy-production.sh"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
GPU_INSTALLER = ROOT / "scripts" / "install-gpu-model.sh"
GPU_COMPOSE = ROOT / "gpu" / "compose.yaml"
RELAY_INSTALLER = ROOT / "scripts" / "install-openai-relay.sh"
PRIVATE_TEST_WORKFLOW = ROOT / ".github" / "workflows" / "test-private-application.yml"


def _load_workflow(path: Path) -> dict:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(document, dict)
    return document


def test_public_ci_uses_no_secrets_and_runs_the_complete_worker_suite() -> None:
    workflow = _load_workflow(CI_WORKFLOW)

    assert workflow["name"] == "Public worker checks"
    assert set(workflow["on"]) == {"push", "pull_request"}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"test"}
    job = workflow["jobs"]["test"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "10"
    assert [step.get("uses") for step in job["steps"] if "uses" in step] == [
        "actions/checkout@v4",
        "actions/setup-python@v5",
    ]
    run_steps = "\n".join(
        str(step.get("run", "")) for step in job["steps"] if "run" in step
    )
    assert "pip install -e '.[test]'" in run_steps
    assert "python -m pytest -q -W error" in run_steps
    assert "bash -n scripts/run-profile-audit-worker.sh" in run_steps
    assert "bash -n scripts/deploy-production.sh" in run_steps
    assert "bash -n scripts/install-gpu-model.sh" in run_steps
    assert "bash -n scripts/install-openai-relay.sh" in run_steps
    assert "secrets." not in str(workflow)


def test_gpu_model_is_pinned_bounded_and_not_publicly_unauthenticated() -> None:
    compose = yaml.safe_load(GPU_COMPOSE.read_text(encoding="utf-8"))
    service = compose["services"]["model"]

    assert service["image"] == "vllm/vllm-openai:v0.23.0"
    assert service["restart"] == "unless-stopped"
    assert service["ports"] == ["8000:8000"]
    command = " ".join(service["command"])
    assert "Qwen/Qwen3-14B-AWQ" in command
    assert "--served-model-name kakpeople-qwen3-14b" in command
    assert "--api-key ${MODEL_API_KEY}" in command
    assert "--max-model-len 8192" in command
    assert "--gpu-memory-utilization 0.90" in command
    assert "--enable-prefix-caching" in command
    assert "--generation-config vllm" in command


def test_gpu_installer_requires_one_source_ip_and_never_echoes_the_key() -> None:
    script = GPU_INSTALLER.read_text(encoding="utf-8")

    assert "APP_SERVER_IP" in script
    assert "MODEL_API_KEY" in script
    assert "DOCKER-USER" in script
    assert "--dport 8000" in script
    assert "-j DROP" in script
    assert "set -x" not in script
    assert "printf '%s' \"$MODEL_API_KEY\"" not in script
    assert "echo \"$MODEL_API_KEY\"" not in script


def test_openai_relay_is_tls_only_source_restricted_and_secret_free() -> None:
    script = RELAY_INSTALLER.read_text(encoding="utf-8")

    assert "APP_SERVER_IP" in script
    assert "RELAY_HOSTNAME" in script
    assert "remote_ip ${APP_SERVER_IP}/32" in script
    assert "reverse_proxy https://api.openai.com" in script
    assert "header_up Host api.openai.com" in script
    assert "respond 403" in script
    assert "ufw allow 80/tcp" in script
    assert "ufw allow 443/tcp" in script
    assert "OPENAI_API_KEY" not in script
    assert "Authorization" not in script
    assert "set -x" not in script
    assert "/dev/stdin" not in script
    assert "chown -R caddy:caddy /var/log/caddy" in script
    assert "runuser --user caddy -- caddy validate" in script
    assert "\ncaddy validate --config" not in script


def test_private_application_checks_are_manual_exact_and_do_not_deploy() -> None:
    workflow = _load_workflow(PRIVATE_TEST_WORKFLOW)

    assert workflow["name"] == "Private application checks"
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {"target_sha"}
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["test"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "30"
    checkouts = [
        step for step in job["steps"] if step.get("uses") == "actions/checkout@v4"
    ]
    assert len(checkouts) == 2
    assert checkouts[1]["with"] == {
        "repository": "AZahahahah/luma",
        "ref": "${{ inputs.target_sha }}",
        "path": "private-app",
        "ssh-key": "${{ secrets.LUMA_READ_SSH_KEY }}",
        "persist-credentials": "false",
    }
    test_step = next(
        step for step in job["steps"] if step.get("name") == "Run private application checks"
    )
    assert test_step["working-directory"] == "private-app"
    serialized = str(workflow)
    assert "pytest tests_web tests -q -W error" in serialized
    assert "npm --prefix frontend test -- --run" in serialized
    assert "npm --prefix frontend run build" in serialized
    assert "REG_RU_" not in serialized
    assert "deploy" not in workflow["jobs"]


def test_profile_audit_workflow_is_manual_bounded_and_oidc_authenticated() -> None:
    workflow = _load_workflow(AUDIT_WORKFLOW)

    assert workflow["name"] == "OpenAI profile worker"
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"target_sha", "operation", "run_id"}
    assert inputs["operation"]["type"] == "choice"
    assert inputs["operation"]["options"] == ["audit", "repair"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "kakpeople-profile-worker",
        "cancel-in-progress": "false",
    }
    assert set(workflow["jobs"]) == {"profile-audit"}
    job = workflow["jobs"]["profile-audit"]
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "360"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert [step.get("uses") for step in job["steps"] if "uses" in step] == [
        "actions/checkout@v4",
        "actions/setup-python@v5",
    ]
    run_steps = "\n".join(
        str(step.get("run", "")) for step in job["steps"] if "run" in step
    )
    assert "pip install ." in run_steps
    assert "bash scripts/run-profile-audit-worker.sh" in run_steps
    assert "upload-artifact" not in str(workflow)
    assert "pull_request" not in workflow["on"]
    assert "schedule" not in workflow["on"]


def _runner_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "RUNNER_TEMP": str(tmp_path),
        "TARGET_SHA": "a" * 40,
        "PROFILE_OPERATION": "audit",
        "AUDIT_RUN_ID": "123e4567-e89b-42d3-a456-426614174000",
        "DEPLOY_HOST": "server.example.test",
        "DEPLOY_USER": "deploy",
        "DEPLOY_KEY": "private-key-placeholder",
        "DEPLOY_KNOWN_HOSTS": "host ssh-ed25519 public-key",
        "OPENAI_API_KEY": "sk-test-private-key-material",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.test/oidc?api-version=1",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("TARGET_SHA", "abc"),
        ("PROFILE_OPERATION", "delete"),
        ("AUDIT_RUN_ID", "../etc/passwd"),
        ("DEPLOY_HOST", "host; id"),
        ("DEPLOY_USER", "root user"),
    ],
)
def test_audit_runner_rejects_unsafe_values_before_network_access(
    tmp_path: Path, field: str, value: str
) -> None:
    environment = _runner_environment(tmp_path)
    environment[field] = value

    result = subprocess.run(
        ["bash", str(AUDIT_RUNNER)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert "private-key-placeholder" not in result.stdout + result.stderr
    assert "sk-test-private-key-material" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "field",
    [
        "RUNNER_TEMP",
        "DEPLOY_KEY",
        "DEPLOY_KNOWN_HOSTS",
        "OPENAI_API_KEY",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
        "ACTIONS_ID_TOKEN_REQUEST_URL",
    ],
)
def test_audit_runner_rejects_missing_secret_inputs_before_network_access(
    tmp_path: Path, field: str
) -> None:
    environment = _runner_environment(tmp_path)
    environment.pop(field)

    result = subprocess.run(
        ["bash", str(AUDIT_RUNNER)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert "private-key-placeholder" not in result.stdout + result.stderr
    assert "sk-test-private-key-material" not in result.stdout + result.stderr


def test_public_workflow_and_runner_contain_no_literal_credentials() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (AUDIT_WORKFLOW, AUDIT_RUNNER, DEPLOY_WORKFLOW, DEPLOY_RUNNER)
    )

    assert "BEGIN OPENSSH PRIVATE KEY" not in combined
    assert "195.19.144.203" not in combined
    assert "sk-proj-" not in combined


def test_deploy_workflow_checks_out_private_head_and_has_bounded_identity() -> None:
    workflow = _load_workflow(DEPLOY_WORKFLOW)

    assert workflow["name"] == "Deploy private application"
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {"target_sha"}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "kakpeople-production-deploy",
        "cancel-in-progress": "false",
    }
    assert set(workflow["jobs"]) == {"deploy"}
    job = workflow["jobs"]["deploy"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "90"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    checkouts = [
        step for step in job["steps"] if step.get("uses") == "actions/checkout@v4"
    ]
    assert len(checkouts) == 2
    private_checkout = checkouts[1]["with"]
    assert private_checkout == {
        "repository": "AZahahahah/luma",
        "ref": "ops/regru-migration",
        "path": "private-app",
        "ssh-key": "${{ secrets.LUMA_READ_SSH_KEY }}",
        "persist-credentials": "false",
    }
    run_steps = "\n".join(
        str(step.get("run", "")) for step in job["steps"] if "run" in step
    )
    assert "bash scripts/deploy-production.sh" in run_steps
    assert "upload-artifact" not in str(workflow)
    assert "pull_request" not in workflow["on"]
    assert "schedule" not in workflow["on"]


def _deploy_environment(tmp_path: Path) -> dict[str, str]:
    private_app = tmp_path / "private-app"
    private_app.mkdir()
    return {
        "PATH": os.environ["PATH"],
        "RUNNER_TEMP": str(tmp_path),
        "PRIVATE_APP_DIR": str(private_app),
        "TARGET_SHA": "a" * 40,
        "DEPLOY_HOST": "server.example.test",
        "DEPLOY_USER": "deploy",
        "DEPLOY_KEY": "private-key-placeholder",
        "DEPLOY_KNOWN_HOSTS": "host ssh-ed25519 public-key",
        "OPENAI_API_KEY": "sk-test-private-key-material",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-token",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.test/oidc?api-version=1",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("TARGET_SHA", "abc"),
        ("DEPLOY_HOST", "host; id"),
        ("DEPLOY_USER", "root user"),
        ("PRIVATE_APP_DIR", "/missing/private-app"),
    ],
)
def test_deploy_runner_rejects_unsafe_values_before_build_or_network(
    tmp_path: Path, field: str, value: str
) -> None:
    environment = _deploy_environment(tmp_path)
    environment[field] = value

    result = subprocess.run(
        ["bash", str(DEPLOY_RUNNER)],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert "private-key-placeholder" not in result.stdout + result.stderr
    assert "sk-test-private-key-material" not in result.stdout + result.stderr
