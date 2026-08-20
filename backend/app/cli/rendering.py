"""Lossless JSON and compact dependency-free human rendering for the CLI."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


def json_output(value: Mapping[str, Any]) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False)


def human_output(tool_name: str, value: Mapping[str, Any]) -> str:
    renderer = _RENDERERS.get(tool_name, _render_document)
    return renderer(value)


def _render_overview(value: Mapping[str, Any]) -> str:
    pipeline = _mapping(value.get("pipeline"))
    recallguard = _mapping(value.get("recallguard"))
    signals = _mapping(value.get("signals"))
    lines = [
        "GapRadar overview",
        f"Pipeline: {display(pipeline.get('state'))}",
        f"RecallGuard: {display(recallguard.get('state'))}",
        (
            "Signals: "
            f"{display(signals.get('trusted', 0))} trusted / "
            f"{display(signals.get('total', 0))} total"
        ),
        (
            "Reliability incidents: "
            f"{display(recallguard.get('active_incident_count', 0))} active, "
            f"{display(recallguard.get('recovered_incident_count', 0))} recovered"
        ),
    ]
    opportunities = _dict_rows(value.get("top_opportunities"))
    if opportunities:
        lines.extend(["", "Top opportunities", _opportunity_table(opportunities)])
    return "\n".join(lines)


def _render_opportunities(value: Mapping[str, Any]) -> str:
    rows = _dict_rows(value.get("opportunities"))
    if not rows:
        return "No opportunities found."
    return "\n".join(
        [f"Opportunities ({display(value.get('count', len(rows)))})", _opportunity_table(rows)]
    )


def _opportunity_table(rows: Sequence[Mapping[str, Any]]) -> str:
    return table(
        ("ID", "Problem", "Score", "Industry"),
        [
            (
                row.get("id"),
                row.get("problem") or row.get("title"),
                row.get("opportunity_score"),
                row.get("industry"),
            )
            for row in rows
        ],
        widths=(36, 48, 8, 24),
    )


def _render_opportunity(value: Mapping[str, Any]) -> str:
    return key_values(
        "Opportunity",
        value,
        (
            ("ID", "id"),
            ("Problem", "problem"),
            ("Description", "description"),
            ("Industry", "industry"),
            ("Opportunity score", "opportunity_score"),
            ("Itch score", "itch_score"),
            ("Severity score", "severity_score"),
            ("TAM score", "tam_score"),
            ("Whitespace score", "whitespace_score"),
            ("Frequency score", "frequency_score"),
            ("Source", "source"),
            ("URL", "canonical_url"),
            ("Observed", "observed_at"),
        ),
    )


def _render_research(value: Mapping[str, Any]) -> str:
    lines = [
        "Persisted academic research",
        f"Papers discovered: {display(value.get('paper_count', 0))}",
        f"Papers matched: {display(value.get('matched_paper_count', 0))}",
        f"Average relevance: {display(value.get('average_relevance_score'))}",
        f"Top concepts: {display(value.get('top_concepts', []))}",
    ]
    papers = _dict_rows(value.get("top_papers"))
    if papers:
        lines.extend(
            [
                "",
                table(
                    ("Title", "Relevance", "Published", "URL"),
                    [
                        (
                            paper.get("title"),
                            paper.get("relevance_score"),
                            paper.get("published_at"),
                            paper.get("paper_url"),
                        )
                        for paper in papers
                    ],
                    widths=(52, 10, 12, 48),
                ),
            ]
        )
    return "\n".join(lines)


def _render_reliability(value: Mapping[str, Any]) -> str:
    collectors = _dict_rows(value.get("collectors"))
    lines = [
        "RecallGuard reliability",
        f"State: {display(value.get('state'))}",
        f"Active incidents: {display(value.get('active_incident_count', 0))}",
        f"Recovered incidents: {display(value.get('recovered_incident_count', 0))}",
    ]
    if collectors:
        lines.extend(
            [
                "",
                table(
                    ("Collector", "Provider", "State", "Last records"),
                    [
                        (
                            row.get("name"),
                            row.get("provider"),
                            row.get("state"),
                            row.get("last_record_count"),
                        )
                        for row in collectors
                    ],
                    widths=(36, 14, 16, 12),
                ),
            ]
        )
    return "\n".join(lines)


def _render_incidents(value: Mapping[str, Any]) -> str:
    incidents = _dict_rows(value.get("incidents"))
    if not incidents:
        return "No reliability incidents found."
    return "\n".join(
        [
            f"Reliability incidents ({display(value.get('count', len(incidents)))})",
            table(
                ("ID", "Status", "Classification", "Action", "Detected"),
                [
                    (
                        row.get("id"),
                        row.get("status"),
                        row.get("classification"),
                        row.get("recommended_action"),
                        row.get("detected_at"),
                    )
                    for row in incidents
                ],
                widths=(36, 15, 20, 18, 24),
            ),
        ]
    )


def _render_investigations(value: Mapping[str, Any]) -> str:
    investigations = _dict_rows(value.get("investigations"))
    if not investigations:
        return "No investigations found."
    return "\n".join(
        [
            f"Investigations ({display(value.get('count', len(investigations)))})",
            table(
                ("ID", "Query", "Industry", "Status", "Created"),
                [
                    (
                        row.get("id"),
                        row.get("query"),
                        row.get("industry"),
                        row.get("status"),
                        row.get("created_at"),
                    )
                    for row in investigations
                ],
                widths=(36, 48, 24, 12, 24),
            ),
        ]
    )


def _render_investigation(value: Mapping[str, Any]) -> str:
    return key_values(
        "Investigation",
        value,
        (
            ("ID", "id"),
            ("Query", "query"),
            ("Industry", "industry"),
            ("Status", "status"),
            ("Created", "created_at"),
            ("Updated", "updated_at"),
        ),
    )


def _render_status(value: Mapping[str, Any]) -> str:
    investigation = _mapping(value.get("investigation"))
    run = value.get("latest_run")
    lines = [
        "Investigation status",
        f"ID: {display(investigation.get('id'))}",
        f"Query: {display(investigation.get('query'))}",
        f"Investigation: {display(investigation.get('status'))}",
    ]
    if not isinstance(run, Mapping):
        lines.append("Latest run: none")
        return "\n".join(lines)
    lines.extend(
        [
            f"Run ID: {display(run.get('run_id'))}",
            f"Run status: {display(run.get('status'))}",
            f"Terminal: {display(run.get('is_terminal'))}",
            f"Retryable: {display(run.get('is_retryable'))}",
        ]
    )
    phases = _mapping(run.get("phases"))
    if phases:
        lines.append(
            "Phases: "
            + ", ".join(
                f"{name}={display(_mapping(details).get('state'))}"
                for name, details in phases.items()
            )
        )
    if run.get("warning"):
        lines.append(f"Warning: {display(run.get('warning'))}")
    if run.get("error"):
        lines.append(f"Error: {display(run.get('error'))}")
    return "\n".join(lines)


def _render_evidence(value: Mapping[str, Any]) -> str:
    evidence = _dict_rows(value.get("evidence"))
    lines = [
        "Persisted demand evidence",
        f"Counts: {display(value.get('counts', {}))}",
    ]
    if evidence:
        lines.append(
            table(
                ("Classification", "Title", "Domain", "Relevance"),
                [
                    (
                        row.get("classification"),
                        row.get("title"),
                        row.get("domain"),
                        row.get("relevance_score"),
                    )
                    for row in evidence
                ],
                widths=(18, 52, 30, 10),
            )
        )
    else:
        lines.append("No accepted supporting or contradicting evidence.")
    return "\n".join(lines)


def _render_competitors(value: Mapping[str, Any]) -> str:
    competitors = _dict_rows(value.get("competitors"))
    lines = [
        "Persisted competitor candidates",
        f"Counts: {display(value.get('counts', {}))}",
    ]
    if competitors:
        lines.append(
            table(
                ("Classification", "Name", "Domain", "Relevance"),
                [
                    (
                        row.get("classification"),
                        row.get("name"),
                        row.get("domain"),
                        row.get("relevance_score"),
                    )
                    for row in competitors
                ],
                widths=(18, 52, 30, 10),
            )
        )
    else:
        lines.append("No accepted competitor candidates.")
    return "\n".join(lines)


def _render_created(value: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "Investigation created",
            f"ID: {display(value.get('id'))}",
            f"Status: {display(value.get('status'))}",
            "Analysis started: no",
        ]
    )


def _render_run(value: Mapping[str, Any]) -> str:
    return "\n".join(
        [
            "Investigation run accepted",
            f"Run ID: {display(value.get('run_id'))}",
            f"Status: {display(value.get('status'))}",
            f"Already running: {display(value.get('already_running'))}",
        ]
    )


def _render_fixture(value: Mapping[str, Any]) -> str:
    return _render_document(value, title="RecallGuard fixture replay (deterministic)")


def _render_live_evidence(value: Mapping[str, Any]) -> str:
    return _render_document(value, title="Live Bright Data evidence (persisted)")


def _render_document(
    value: Mapping[str, Any],
    *,
    title: str = "GapRadar result",
) -> str:
    lines = [title]
    for key, item in value.items():
        label = key.replace("_", " ").capitalize()
        if isinstance(item, Mapping | list):
            lines.append(f"{label}: {json.dumps(item, indent=2, ensure_ascii=False)}")
        else:
            lines.append(f"{label}: {display(item)}")
    return "\n".join(lines)


def key_values(
    title: str,
    value: Mapping[str, Any],
    fields: Iterable[tuple[str, str]],
) -> str:
    return "\n".join(
        [title, *(f"{label}: {display(value.get(key))}" for label, key in fields)]
    )


def table(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    widths: Sequence[int],
) -> str:
    rendered = [[truncate(display(cell), width) for cell, width in zip(row, widths)] for row in rows]
    all_rows = [list(headers), *rendered]
    actual_widths = [
        min(width, max(len(str(row[index])) for row in all_rows))
        for index, width in enumerate(widths)
    ]

    def line(row: Sequence[Any]) -> str:
        return "  ".join(
            truncate(str(cell), width).ljust(width)
            for cell, width in zip(row, actual_widths)
        ).rstrip()

    return "\n".join([line(headers), line(tuple("-" * width for width in actual_widths)), *(line(row) for row in rendered)])


def display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Mapping):
        return ", ".join(f"{key}={display(item)}" for key, item in value.items()) or "—"
    if isinstance(value, list):
        return ", ".join(display(item) for item in value) or "—"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return f"{value[: max(0, width - 1)]}…"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _dict_rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


_RENDERERS = {
    "get_gapradar_overview": _render_overview,
    "list_opportunities": _render_opportunities,
    "get_opportunity": _render_opportunity,
    "get_opportunity_research": _render_research,
    "get_reliability_overview": _render_reliability,
    "list_reliability_incidents": _render_incidents,
    "get_reliability_incident": _render_document,
    "get_recallguard_demo": _render_fixture,
    "get_live_brightdata_evidence": _render_live_evidence,
    "list_investigations": _render_investigations,
    "get_investigation": _render_investigation,
    "get_investigation_status": _render_status,
    "get_investigation_research": _render_research,
    "get_investigation_demand_evidence": _render_evidence,
    "get_investigation_competitors": _render_competitors,
    "create_investigation": _render_created,
    "run_investigation": _render_run,
}
