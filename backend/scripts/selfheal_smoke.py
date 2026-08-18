"""Run ONE real Bright Data self-healing attempt, by hand.

Never imported by the application and never executed by pytest. It exists
so a demo can drive the real provider against a deliberately broken
DEVELOPMENT collector and watch RecallGuard decide.

    cd backend
    uv run python scripts/selfheal_smoke.py \
        --collector-id <uuid of the Collector row> \
        --i-understand-this-calls-bright-data

Credentials come from the environment the app already uses
(BRIGHTDATA_API_KEY). The token is never printed, logged, or echoed back.

The equivalent CLI narration, for a demo, is:

    brightdata scraper heal <external_collector_id> "<prompt>"
    brightdata scraper approve <external_collector_id>
    brightdata scraper approve <external_collector_id> --reject

--auto-approve is deliberately not used: the whole point is that
RecallGuard, not the provider, decides whether a candidate is worth
approving, and only a fresh production run can prove it worked.
"""

import argparse
import sys
import uuid
from pathlib import Path

# The `app` package is not installed into the virtualenv -- it is
# importable because the backend directory is normally the working
# directory. A script run from backend/scripts/ gets its own directory on
# sys.path instead, so the backend directory is added explicitly here.
# This must precede every `app.` import below.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Collector
from app.integrations.brightdata.client import BrightDataClient
from app.logging_config import configure_logging
from app.recallguard.healing import execute_healing_attempt
from app.recallguard.schemas import BaselineProfile
from app.recallguard.service import active_incident


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run ONE real Bright Data self-healing attempt by hand.",
        epilog=(
            "This makes live provider calls: it triggers a self-healing job, "
            "decides whether to approve the candidate, and -- if approved -- "
            "runs a real production collection to verify it.\n"
            "Point it at a deliberately broken DEVELOPMENT collector, never "
            "the verified production one.\n\n"
            "Credentials come from the environment (BRIGHTDATA_API_KEY) and "
            "are never printed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--collector-id", required=True, help="Collector row id (UUID)")
    parser.add_argument(
        "--baseline-records",
        type=int,
        required=True,
        help="Observed healthy record count to compare completeness against",
    )
    parser.add_argument(
        "--i-understand-this-calls-bright-data",
        action="store_true",
        help="Required. This triggers a real self-healing job and a real collection.",
    )
    args = parser.parse_args()

    if not args.i_understand_this_calls_bright_data:
        print("Refusing to run: this makes live Bright Data calls.", file=sys.stderr)
        return 2

    settings = get_settings()
    if not settings.BRIGHTDATA_API_KEY:
        print("BRIGHTDATA_API_KEY is not set.", file=sys.stderr)
        return 2

    configure_logging(settings.APP_ENV)
    engine = create_engine(settings.DATABASE_URL)

    with Session(engine) as session, BrightDataClient(settings=settings) as client:
        collector = session.get(Collector, uuid.UUID(args.collector_id))
        if collector is None:
            print(f"No collector {args.collector_id}", file=sys.stderr)
            return 1

        incident = active_incident(session, collector_id=collector.id)
        if incident is None:
            print(
                "No active incident for this collector; nothing to heal. Run a "
                "collection and evaluate it first.",
                file=sys.stderr,
            )
            return 1

        print(
            f"collector={collector.external_collector_id} "
            f"incident={incident.id} status={incident.status.value} "
            f"attempts={incident.repair_attempts}"
        )
        result = execute_healing_attempt(
            session,
            client,
            incident=incident,
            collector=collector,
            baseline=BaselineProfile(
                label="manual_smoke", record_count=args.baseline_records
            ),
        )

    print(
        f"outcome={result.outcome.value} attempt={result.attempt} "
        f"approved={result.candidate_approved} recovered={result.recovered} "
        f"verification_run={result.verification_run_id}"
    )
    if result.detail:
        print(f"detail={result.detail}")
    return 0 if result.recovered else 1


if __name__ == "__main__":
    raise SystemExit(main())
