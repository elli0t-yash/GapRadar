"""The `gapradar` MCP-backed command tree."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from uuid import UUID

import click

from app.cli.client import (
    AuthenticationError,
    ConfigurationError,
    ConnectionError,
    GapRadarClient,
    MCPConnectionConfig,
    ToolCallError,
    ToolContractError,
    ToolUnavailableError,
)
from app.cli.rendering import human_output, json_output

EXIT_CONFIG_OR_CONNECTION = 3
EXIT_DOMAIN_OR_TOOL = 4
EXIT_INTERNAL = 5
DEFAULT_WATCH_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True)
class CLIState:
    url_override: str | None


CLIENT_FACTORY = GapRadarClient


class CLIError(click.ClickException):
    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def stdin_is_interactive() -> bool:
    return sys.stdin.isatty()


def output_option(function):
    return click.option(
        "--json",
        "json_mode",
        is_flag=True,
        help="Emit the MCP structured result as JSON.",
    )(function)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--url",
    "url_override",
    metavar="URL",
    help="Override GAPRADAR_MCP_URL for this invocation.",
)
@click.pass_context
def cli(context: click.Context, url_override: str | None) -> None:
    """Inspect and operate GapRadar through authenticated MCP tools."""

    context.obj = CLIState(url_override=url_override)


@cli.command("version")
@output_option
def version_command(json_mode: bool) -> None:
    """Show the installed GapRadar CLI version."""

    try:
        installed = version("backend")
    except PackageNotFoundError:  # pragma: no cover - packaged entrypoint installs it
        installed = "0.1.0"
    value = {"version": installed}
    click.echo(json_output(value) if json_mode else f"GapRadar CLI {installed}")


@cli.command("overview")
@output_option
@click.pass_obj
def overview(state: CLIState, json_mode: bool) -> None:
    """Show persisted product, pipeline, and RecallGuard intelligence."""

    _call_and_render(state, "get_gapradar_overview", {}, json_mode=json_mode)


@cli.group("opportunities")
def opportunities_group() -> None:
    """Read trusted persisted opportunities and their research."""


@opportunities_group.command("list")
@click.option("--limit", type=click.IntRange(1, 200), default=20, show_default=True)
@output_option
@click.pass_obj
def opportunities_list(state: CLIState, limit: int, json_mode: bool) -> None:
    """List the strongest currently trusted opportunities."""

    _call_and_render(
        state,
        "list_opportunities",
        {"limit": limit},
        json_mode=json_mode,
    )


@opportunities_group.command("show")
@click.argument("opportunity_id", type=click.UUID)
@output_option
@click.pass_obj
def opportunity_show(state: CLIState, opportunity_id: UUID, json_mode: bool) -> None:
    """Show one persisted trusted opportunity."""

    _call_and_render(
        state,
        "get_opportunity",
        {"opportunity_id": str(opportunity_id)},
        json_mode=json_mode,
    )


@opportunities_group.command("research")
@click.argument("opportunity_id", type=click.UUID)
@output_option
@click.pass_obj
def opportunity_research(
    state: CLIState,
    opportunity_id: UUID,
    json_mode: bool,
) -> None:
    """Read persisted academic research for an opportunity."""

    _call_and_render(
        state,
        "get_opportunity_research",
        {"opportunity_id": str(opportunity_id)},
        json_mode=json_mode,
    )


@cli.group("reliability")
def reliability_group() -> None:
    """Inspect persisted RecallGuard health and evidence."""


@reliability_group.command("overview")
@output_option
@click.pass_obj
def reliability_overview(state: CLIState, json_mode: bool) -> None:
    """Show current persisted collector reliability."""

    _call_and_render(state, "get_reliability_overview", {}, json_mode=json_mode)


@reliability_group.command("incidents")
@output_option
@click.pass_obj
def reliability_incidents(state: CLIState, json_mode: bool) -> None:
    """List persisted reliability incidents."""

    _call_and_render(state, "list_reliability_incidents", {}, json_mode=json_mode)


@reliability_group.command("incident")
@click.argument("incident_id", type=click.UUID)
@output_option
@click.pass_obj
def reliability_incident(
    state: CLIState,
    incident_id: UUID,
    json_mode: bool,
) -> None:
    """Show one persisted reliability incident and its proof."""

    _call_and_render(
        state,
        "get_reliability_incident",
        {"incident_id": str(incident_id)},
        json_mode=json_mode,
    )


@reliability_group.command("demo")
@output_option
@click.pass_obj
def reliability_demo(state: CLIState, json_mode: bool) -> None:
    """Read deterministic RecallGuard fixture-replay evidence."""

    _call_and_render(state, "get_recallguard_demo", {}, json_mode=json_mode)


@reliability_group.command("brightdata")
@output_option
@click.pass_obj
def reliability_brightdata(state: CLIState, json_mode: bool) -> None:
    """Read persisted evidence from the isolated real Bright Data experiment."""

    _call_and_render(
        state,
        "get_live_brightdata_evidence",
        {},
        json_mode=json_mode,
    )


@cli.group("investigations")
def investigations_group() -> None:
    """Create, run, and read independent Investigations."""


@investigations_group.command("list")
@click.option("--limit", type=click.IntRange(1, 200), default=20, show_default=True)
@output_option
@click.pass_obj
def investigations_list(state: CLIState, limit: int, json_mode: bool) -> None:
    """List persisted Investigations without starting analysis."""

    _call_and_render(
        state,
        "list_investigations",
        {"limit": limit},
        json_mode=json_mode,
    )


@investigations_group.command("show")
@click.argument("investigation_id", type=click.UUID)
@output_option
@click.pass_obj
def investigation_show(
    state: CLIState,
    investigation_id: UUID,
    json_mode: bool,
) -> None:
    """Show one persisted Investigation without starting analysis."""

    _call_and_render(
        state,
        "get_investigation",
        {"investigation_id": str(investigation_id)},
        json_mode=json_mode,
    )


@investigations_group.command("status")
@click.argument("investigation_id", type=click.UUID)
@click.option("--watch", is_flag=True, help="Poll persisted status until terminal.")
@click.option(
    "--interval",
    type=click.FloatRange(min=0.1),
    default=DEFAULT_WATCH_INTERVAL_SECONDS,
    show_default=True,
    help="Watch polling interval in seconds.",
)
@output_option
@click.pass_obj
def investigation_status(
    state: CLIState,
    investigation_id: UUID,
    watch: bool,
    interval: float,
    json_mode: bool,
) -> None:
    """Read the latest persisted run state; optionally watch it."""

    arguments = {"investigation_id": str(investigation_id)}
    if not watch:
        _call_and_render(
            state,
            "get_investigation_status",
            arguments,
            json_mode=json_mode,
        )
        return

    try:
        _run_async(
            _watch_status(
                state,
                arguments,
                interval=interval,
                json_mode=json_mode,
            )
        )
    except KeyboardInterrupt:
        click.echo("Stopped watching Investigation status.", err=True)
        raise click.exceptions.Exit(130) from None


@investigations_group.command("research")
@click.argument("investigation_id", type=click.UUID)
@output_option
@click.pass_obj
def investigation_research(
    state: CLIState,
    investigation_id: UUID,
    json_mode: bool,
) -> None:
    """Read persisted Investigation research without acquiring new papers."""

    _call_and_render(
        state,
        "get_investigation_research",
        {"investigation_id": str(investigation_id)},
        json_mode=json_mode,
    )


@investigations_group.command("evidence")
@click.argument("investigation_id", type=click.UUID)
@click.option("--limit", type=click.IntRange(1, 200), default=50, show_default=True)
@output_option
@click.pass_obj
def investigation_evidence(
    state: CLIState,
    investigation_id: UUID,
    limit: int,
    json_mode: bool,
) -> None:
    """Read persisted supporting and contradicting demand evidence."""

    _call_and_render(
        state,
        "get_investigation_demand_evidence",
        {"investigation_id": str(investigation_id), "limit": limit},
        json_mode=json_mode,
    )


@investigations_group.command("competitors")
@click.argument("investigation_id", type=click.UUID)
@click.option("--limit", type=click.IntRange(1, 200), default=50, show_default=True)
@output_option
@click.pass_obj
def investigation_competitors(
    state: CLIState,
    investigation_id: UUID,
    limit: int,
    json_mode: bool,
) -> None:
    """Read persisted competitor candidates without new discovery."""

    _call_and_render(
        state,
        "get_investigation_competitors",
        {"investigation_id": str(investigation_id), "limit": limit},
        json_mode=json_mode,
    )


@investigations_group.command("create")
@click.argument("query")
@click.option("--industry", default=None, help="Optional user-supplied industry.")
@output_option
@click.pass_obj
def investigation_create(
    state: CLIState,
    query: str,
    industry: str | None,
    json_mode: bool,
) -> None:
    """Create a DRAFT Investigation; analysis is not started."""

    _call_and_render(
        state,
        "create_investigation",
        {"query": query, "industry": industry},
        json_mode=json_mode,
        write=True,
    )


@investigations_group.command("run")
@click.argument("investigation_id", type=click.UUID)
@click.option("--yes", is_flag=True, help="Confirm provider-spending analysis.")
@output_option
@click.pass_obj
def investigation_run(
    state: CLIState,
    investigation_id: UUID,
    yes: bool,
    json_mode: bool,
) -> None:
    """Explicitly start or reuse analysis for an Investigation."""

    if not yes:
        click.echo(
            "This may use Bright Data, OpenAI, and academic research providers.",
            err=True,
        )
        if not stdin_is_interactive():
            raise click.UsageError(
                "Confirmation requires an interactive terminal. Re-run with --yes."
            )
        if not click.confirm("Start Investigation analysis?", default=False, err=True):
            if json_mode:
                click.echo(json_output({"started": False, "reason": "cancelled"}))
            else:
                click.echo("Run cancelled.")
            return

    _call_and_render(
        state,
        "run_investigation",
        {"investigation_id": str(investigation_id)},
        json_mode=json_mode,
        write=True,
        ambiguous_status_id=str(investigation_id),
    )


def _call_and_render(
    state: CLIState,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    json_mode: bool,
    write: bool = False,
    ambiguous_status_id: str | None = None,
) -> None:
    value = _run_async(
        _call_tool(state, tool_name, arguments, write=write),
        ambiguous_status_id=ambiguous_status_id,
    )
    _emit(tool_name, value, json_mode=json_mode)


async def _call_tool(
    state: CLIState,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    write: bool,
) -> dict[str, Any]:
    config = MCPConnectionConfig.from_environment(url_override=state.url_override)
    async with CLIENT_FACTORY(config) as client:
        return await client.call_tool(tool_name, arguments, write=write)


async def _watch_status(
    state: CLIState,
    arguments: dict[str, Any],
    *,
    interval: float,
    json_mode: bool,
) -> None:
    config = MCPConnectionConfig.from_environment(url_override=state.url_override)
    async with CLIENT_FACTORY(config) as client:
        first = True
        while True:
            value = await client.call_tool(
                "get_investigation_status",
                arguments,
                write=False,
            )
            latest_run = value.get("latest_run")
            terminal = (
                isinstance(latest_run, dict)
                and latest_run.get("is_terminal") is True
            )
            if not json_mode:
                if not first:
                    click.echo("\n---")
                _emit("get_investigation_status", value, json_mode=False)
            if latest_run is None or terminal:
                if json_mode:
                    _emit("get_investigation_status", value, json_mode=True)
                return
            first = False
            await asyncio.sleep(interval)


def _run_async(coroutine, *, ambiguous_status_id: str | None = None):
    try:
        return asyncio.run(coroutine)
    except (ConfigurationError, AuthenticationError) as exc:
        raise CLIError(str(exc), exit_code=EXIT_CONFIG_OR_CONNECTION) from None
    except ConnectionError as exc:
        message = str(exc)
        if exc.action_may_have_completed and ambiguous_status_id is not None:
            message += (
                " The run request may have reached GapRadar; do not retry it "
                "automatically. Inspect it with: gapradar investigations status "
                f"{ambiguous_status_id}"
            )
        raise CLIError(message, exit_code=EXIT_CONFIG_OR_CONNECTION) from None
    except (ToolUnavailableError, ToolCallError, ToolContractError) as exc:
        raise CLIError(str(exc), exit_code=EXIT_DOMAIN_OR_TOOL) from None
    except KeyboardInterrupt:
        raise
    except Exception:  # noqa: BLE001 - final sanitized CLI boundary
        raise CLIError(
            "GapRadar CLI failed unexpectedly.",
            exit_code=EXIT_INTERNAL,
        ) from None


def _emit(tool_name: str, value: dict[str, Any], *, json_mode: bool) -> None:
    try:
        output = json_output(value) if json_mode else human_output(tool_name, value)
    except Exception:  # noqa: BLE001 - sanitize rendering/contract drift
        raise CLIError(
            "GapRadar CLI failed unexpectedly.",
            exit_code=EXIT_INTERNAL,
        ) from None
    click.echo(output)


if __name__ == "__main__":  # pragma: no cover
    cli()
