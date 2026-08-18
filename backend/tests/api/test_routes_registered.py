"""The app starts and exposes exactly the surface the frontend needs.

Guards against a route module being written but never included, and
against a CRUD surface quietly appearing: the frontend reads, and writes
only by asking the pipeline to run.

Asking the pipeline to run is a claim, not the work: POST answers 202
with an execution id and the client polls GET /pipeline/runs/{id}. There
is deliberately no endpoint that mutates an execution or an incident.
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
    ("GET", "/api/v1/reliability"),
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
