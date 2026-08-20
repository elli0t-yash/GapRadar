"""The app starts and exposes exactly the surface the frontend needs.

Guards against a route module being written but never included, and
against a CRUD surface quietly appearing: the frontend reads, and writes
only by asking the pipeline to run or by advancing the explicitly isolated
RecallGuard fixture replay.

Asking the pipeline to run is a claim, not the work: POST answers 202
with an execution id and the client polls GET /pipeline/runs/{id}. There
is deliberately no endpoint that mutates an execution or an incident.

Research intelligence is read-only for the same reason: enrichment
acquires from a provider and judges relevance, so it is never reachable
through a GET.

Investigations are the one place a user writes free text. POST
/investigations creates a record and starts NOTHING -- 201, no provider
call, no scheduler -- which is why it is allowed to exist on a surface
that otherwise refuses CRUD. There is deliberately no update or delete.

POST /investigations/{id}/run is the one endpoint here that costs money,
and it is a 202 claim rather than the work, exactly like the pipeline and
enrichment claims above it. Its GET twin, /research, /evidence and
/competitors are pure reads -- and none of them opens a discovered URL.

Web evidence and competitors are separate endpoints rather than fields on
one investigation payload: a caller that wants competitors should not pay
for demand evidence, and one combined response would grow without bound
as phases are added.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.factory import create_app

EXPECTED_PATHS = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/dashboard"),
    ("GET", "/api/v1/opportunities"),
    ("GET", "/api/v1/opportunities/{signal_id}"),
    ("GET", "/api/v1/opportunities/{signal_id}/research"),
    ("POST", "/api/v1/opportunities/{signal_id}/research/enrich"),
    ("GET", "/api/v1/opportunities/{signal_id}/research/enrichment"),
    ("POST", "/api/v1/investigations"),
    ("GET", "/api/v1/investigations"),
    ("GET", "/api/v1/investigations/{investigation_id}"),
    ("POST", "/api/v1/investigations/{investigation_id}/run"),
    ("GET", "/api/v1/investigations/{investigation_id}/run"),
    ("GET", "/api/v1/investigations/{investigation_id}/research"),
    ("GET", "/api/v1/investigations/{investigation_id}/evidence"),
    ("GET", "/api/v1/investigations/{investigation_id}/competitors"),
    ("GET", "/api/v1/reliability"),
    ("GET", "/api/v1/reliability/live-evidence"),
    ("GET", "/api/v1/reliability/demo"),
    ("POST", "/api/v1/reliability/demo/start"),
    ("POST", "/api/v1/reliability/demo/advance"),
    ("GET", "/api/v1/reliability/incidents"),
    ("GET", "/api/v1/reliability/incidents/{incident_id}"),
    ("GET", "/api/v1/collectors"),
    ("GET", "/api/v1/collectors/{collector_id}/runs"),
    ("POST", "/api/v1/pipeline/run"),
    ("GET", "/api/v1/pipeline/runs/{pipeline_run_id}"),
}


def routes(app: FastAPI) -> set[tuple[str, str]]:
    return {
        (method, path)
        for path, operations in app.openapi()["paths"].items()
        for method in (operation.upper() for operation in operations)
    }


def test_the_v1_surface_is_exactly_what_the_frontend_needs(
    settings: Settings,
) -> None:
    assert routes(create_app(settings=settings)) == EXPECTED_PATHS


def test_the_openapi_schema_renders(api_client: TestClient) -> None:
    """A broken response model would fail here rather than at request time."""
    assert api_client.get("/openapi.json").status_code == 200
