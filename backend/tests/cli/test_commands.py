"""Behavior and exact MCP mappings for the complete command tree."""

from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from app.cli import main
from app.cli.client import (
    AuthenticationError,
    ConnectionError,
    ToolCallError,
    ToolUnavailableError,
)
from app.cli.main import cli
from tests.cli.conftest import TEST_TOKEN, RecordingClient

OPPORTUNITY_ID = "11111111-1111-4111-8111-111111111111"
INVESTIGATION_ID = "22222222-2222-4222-8222-222222222222"
RUN_ID = "33333333-3333-4333-8333-333333333333"
INCIDENT_ID = "44444444-4444-4444-8444-444444444444"


def test_root_help_and_version_need_no_connection(runner: CliRunner) -> None:
    help_result = runner.invoke(cli, ["--help"], env={})
    version_result = runner.invoke(cli, ["version", "--json"], env={})

    assert help_result.exit_code == 0
    assert "opportunities" in help_result.output
    assert "reliability" in help_result.output
    assert "investigations" in help_result.output
    assert json.loads(version_result.stdout) == {"version": "0.1.0"}


def test_missing_configuration_uses_exit_three(runner: CliRunner) -> None:
    result = runner.invoke(
        cli,
        ["overview"],
        env={"GAPRADAR_MCP_URL": "", "GAPRADAR_MCP_API_KEY": ""},
    )

    assert result.exit_code == 3
    assert "Set GAPRADAR_MCP_URL" in result.stderr


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (AuthenticationError("authentication failed"), "authentication failed"),
        (ConnectionError("server unavailable"), "server unavailable"),
    ],
)
def test_auth_and_connection_failures_use_exit_three_without_leaking_token(
    runner: CliRunner,
    recording_client: RecordingClient,
    error: Exception,
    message: str,
) -> None:
    recording_client.enter_error = error

    result = runner.invoke(cli, ["overview"])

    assert result.exit_code == 3
    assert message in result.stderr
    assert TEST_TOKEN not in result.output
    assert TEST_TOKEN not in result.stderr


@pytest.mark.parametrize(
    "error",
    [
        ToolUnavailableError(
            "GapRadar server does not expose required tool: get_gapradar_overview"
        ),
        ToolCallError("Opportunity was not found."),
    ],
)
def test_tool_and_domain_failures_use_exit_four(
    runner: CliRunner,
    recording_client: RecordingClient,
    error: Exception,
) -> None:
    recording_client.errors["get_gapradar_overview"] = error

    result = runner.invoke(cli, ["overview"])

    assert result.exit_code == 4
    assert str(error) in result.stderr


def test_unexpected_rendering_failure_uses_exit_five(
    runner: CliRunner,
    recording_client: RecordingClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_render(*args: object, **kwargs: object) -> str:
        raise ValueError("secret detail")

    monkeypatch.setattr(main, "human_output", fail_render)

    result = runner.invoke(cli, ["overview"])

    assert result.exit_code == 5
    assert "failed unexpectedly" in result.stderr
    assert "secret detail" not in result.stderr


def test_url_override_is_forwarded_without_a_secret_flag(
    runner: CliRunner,
    recording_client: RecordingClient,
) -> None:
    result = runner.invoke(
        cli,
        ["--url", "https://override.example/mcp", "overview"],
    )
    rejected_secret = runner.invoke(cli, ["--api-key", TEST_TOKEN, "overview"])

    assert result.exit_code == 0
    assert rejected_secret.exit_code == 2
    assert recording_client.calls == [("get_gapradar_overview", {}, False)]


def test_overview_human_and_json_output(
    runner: CliRunner,
    recording_client: RecordingClient,
) -> None:
    payload = {
        "pipeline": {"state": "healthy"},
        "recallguard": {
            "state": "healthy",
            "active_incident_count": 0,
            "recovered_incident_count": 2,
        },
        "signals": {"total": 9, "trusted": 8},
        "top_opportunities": [],
    }
    recording_client.responses["get_gapradar_overview"] = payload

    human = runner.invoke(cli, ["overview"])
    structured = runner.invoke(cli, ["overview", "--json"])

    assert human.exit_code == 0
    assert "Pipeline: healthy" in human.stdout
    assert json.loads(structured.stdout) == payload


@pytest.mark.parametrize(
    ("arguments", "tool", "tool_arguments"),
    [
        (
            ["opportunities", "list", "--limit", "7", "--json"],
            "list_opportunities",
            {"limit": 7},
        ),
        (
            ["opportunities", "show", OPPORTUNITY_ID, "--json"],
            "get_opportunity",
            {"opportunity_id": OPPORTUNITY_ID},
        ),
        (
            ["opportunities", "research", OPPORTUNITY_ID, "--json"],
            "get_opportunity_research",
            {"opportunity_id": OPPORTUNITY_ID},
        ),
    ],
)
def test_opportunity_commands_map_exactly_to_read_tools(
    runner: CliRunner,
    recording_client: RecordingClient,
    arguments: list[str],
    tool: str,
    tool_arguments: dict[str, object],
) -> None:
    recording_client.responses[tool] = {"tool": tool}

    result = runner.invoke(cli, arguments)

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"tool": tool}
    assert recording_client.calls == [(tool, tool_arguments, False)]


@pytest.mark.parametrize(
    ("arguments", "tool", "tool_arguments"),
    [
        (["reliability", "overview", "--json"], "get_reliability_overview", {}),
        (
            ["reliability", "incidents", "--json"],
            "list_reliability_incidents",
            {},
        ),
        (
            ["reliability", "incident", INCIDENT_ID, "--json"],
            "get_reliability_incident",
            {"incident_id": INCIDENT_ID},
        ),
        (["reliability", "demo", "--json"], "get_recallguard_demo", {}),
        (
            ["reliability", "brightdata", "--json"],
            "get_live_brightdata_evidence",
            {},
        ),
    ],
)
def test_reliability_commands_map_exactly_to_persisted_read_tools(
    runner: CliRunner,
    recording_client: RecordingClient,
    arguments: list[str],
    tool: str,
    tool_arguments: dict[str, object],
) -> None:
    recording_client.responses[tool] = {"tool": tool}

    result = runner.invoke(cli, arguments)

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"tool": tool}
    assert recording_client.calls == [(tool, tool_arguments, False)]


@pytest.mark.parametrize(
    ("arguments", "tool", "tool_arguments"),
    [
        (
            ["investigations", "list", "--limit", "9", "--json"],
            "list_investigations",
            {"limit": 9},
        ),
        (
            ["investigations", "show", INVESTIGATION_ID, "--json"],
            "get_investigation",
            {"investigation_id": INVESTIGATION_ID},
        ),
        (
            ["investigations", "status", INVESTIGATION_ID, "--json"],
            "get_investigation_status",
            {"investigation_id": INVESTIGATION_ID},
        ),
        (
            ["investigations", "research", INVESTIGATION_ID, "--json"],
            "get_investigation_research",
            {"investigation_id": INVESTIGATION_ID},
        ),
        (
            [
                "investigations",
                "evidence",
                INVESTIGATION_ID,
                "--limit",
                "12",
                "--json",
            ],
            "get_investigation_demand_evidence",
            {"investigation_id": INVESTIGATION_ID, "limit": 12},
        ),
        (
            [
                "investigations",
                "competitors",
                INVESTIGATION_ID,
                "--limit",
                "13",
                "--json",
            ],
            "get_investigation_competitors",
            {"investigation_id": INVESTIGATION_ID, "limit": 13},
        ),
    ],
)
def test_investigation_read_commands_never_start_analysis(
    runner: CliRunner,
    recording_client: RecordingClient,
    arguments: list[str],
    tool: str,
    tool_arguments: dict[str, object],
) -> None:
    recording_client.responses[tool] = {"tool": tool}

    result = runner.invoke(cli, arguments)

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"tool": tool}
    assert recording_client.calls == [(tool, tool_arguments, False)]
    assert all(call[0] != "run_investigation" for call in recording_client.calls)


def test_create_persists_draft_without_running_analysis(
    runner: CliRunner,
    recording_client: RecordingClient,
) -> None:
    recording_client.responses["create_investigation"] = {
        "id": INVESTIGATION_ID,
        "query": "AI compliance assistant",
        "industry": "B2B SaaS",
        "status": "draft",
    }

    result = runner.invoke(
        cli,
        [
            "investigations",
            "create",
            "AI compliance assistant",
            "--industry",
            "B2B SaaS",
        ],
    )

    assert result.exit_code == 0
    assert "Investigation created" in result.stdout
    assert "Analysis started: no" in result.stdout
    assert recording_client.calls == [
        (
            "create_investigation",
            {"query": "AI compliance assistant", "industry": "B2B SaaS"},
            True,
        )
    ]
    assert all(call[0] != "run_investigation" for call in recording_client.calls)


def test_run_decline_never_calls_mcp(
    runner: CliRunner,
    recording_client: RecordingClient,
    make_interactive: Callable[[], None],
) -> None:
    make_interactive()

    result = runner.invoke(
        cli,
        ["investigations", "run", INVESTIGATION_ID],
        input="n\n",
    )

    assert result.exit_code == 0
    assert "Run cancelled" in result.stdout
    assert recording_client.calls == []


def test_noninteractive_run_requires_yes_and_never_calls_mcp(
    runner: CliRunner,
    recording_client: RecordingClient,
) -> None:
    result = runner.invoke(cli, ["investigations", "run", INVESTIGATION_ID])

    assert result.exit_code == 2
    assert "Re-run with --yes" in result.stderr
    assert recording_client.calls == []


@pytest.mark.parametrize("confirmation", ["interactive", "yes"])
def test_run_calls_provider_spending_tool_exactly_once_after_confirmation(
    runner: CliRunner,
    recording_client: RecordingClient,
    make_interactive: Callable[[], None],
    confirmation: str,
) -> None:
    recording_client.responses["run_investigation"] = {
        "run_id": RUN_ID,
        "investigation_id": INVESTIGATION_ID,
        "status": "queued",
        "already_running": False,
    }
    arguments = ["investigations", "run", INVESTIGATION_ID, "--json"]
    input_text = None
    if confirmation == "yes":
        arguments.append("--yes")
    else:
        make_interactive()
        input_text = "y\n"

    result = runner.invoke(cli, arguments, input=input_text)

    assert result.exit_code == 0
    assert json.loads(result.stdout)["run_id"] == RUN_ID
    assert recording_client.calls == [
        (
            "run_investigation",
            {"investigation_id": INVESTIGATION_ID},
            True,
        )
    ]


def test_ambiguous_run_failure_warns_not_to_retry(
    runner: CliRunner,
    recording_client: RecordingClient,
) -> None:
    recording_client.errors["run_investigation"] = ConnectionError(
        "connection dropped",
        action_may_have_completed=True,
    )

    result = runner.invoke(
        cli,
        ["investigations", "run", INVESTIGATION_ID, "--yes"],
    )

    assert result.exit_code == 3
    assert "do not retry it automatically" in result.stderr
    assert f"investigations status {INVESTIGATION_ID}" in result.stderr
    assert len(recording_client.calls) == 1


def status_payload(*, status: str, terminal: bool) -> dict[str, object]:
    return {
        "investigation": {
            "id": INVESTIGATION_ID,
            "query": "Test hypothesis",
            "status": "running" if not terminal else "succeeded",
        },
        "latest_run": {
            "run_id": RUN_ID,
            "status": status,
            "is_terminal": terminal,
            "is_retryable": False,
            "phases": {},
        },
    }


def test_watch_reads_running_then_terminal_without_starting_a_run(
    runner: CliRunner,
    recording_client: RecordingClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording_client.responses["get_investigation_status"] = [
        status_payload(status="running", terminal=False),
        status_payload(status="succeeded", terminal=True),
    ]
    sleep = AsyncMock()
    monkeypatch.setattr(main.asyncio, "sleep", sleep)

    result = runner.invoke(
        cli,
        ["investigations", "status", INVESTIGATION_ID, "--watch", "--json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["latest_run"]["is_terminal"] is True
    assert [call[0] for call in recording_client.calls] == [
        "get_investigation_status",
        "get_investigation_status",
    ]
    assert all(call[0] != "run_investigation" for call in recording_client.calls)
    sleep.assert_awaited_once_with(2.0)


def test_watch_ctrl_c_exits_cleanly_without_starting_a_run(
    runner: CliRunner,
    recording_client: RecordingClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recording_client.responses["get_investigation_status"] = status_payload(
        status="running", terminal=False
    )
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock(side_effect=KeyboardInterrupt))

    result = runner.invoke(
        cli,
        ["investigations", "status", INVESTIGATION_ID, "--watch"],
    )

    assert result.exit_code == 130
    assert "Stopped watching" in result.stderr
    assert [call[0] for call in recording_client.calls] == [
        "get_investigation_status"
    ]
