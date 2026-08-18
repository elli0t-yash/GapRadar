"""Undo ONE incident's escalation caused by the missing-preview bug.

A single-purpose repair for a single row, written to be deleted once it
has been run.

The bug: candidate preflight used to treat "the provider offered no
preview_result" as a reason to escalate, so incident
ae20c718-55b9-4fa3-9bd9-31b78f23495e went to MANUAL_REVIEW / ESCALATE on
attempt 1 of 3 instead of rejecting the candidate and trying again. The
preflight fix (app.recallguard.healing) makes that impossible going
forward, but it cannot rescue the row it already stranded: MANUAL_REVIEW
is exactly the state autonomous repair refuses to touch, by design, so
that incident will sit there forever unless a human puts it back.

What this restores is the state the incident SHOULD have been left in:

    status             MANUAL_REVIEW -> DEGRADED
    recommended_action ESCALATE      -> REQUEST_HEAL

and nothing else. In particular repair_attempts stays at 1. Attempt 1 was
genuinely spent -- a real repair ran, reached the gate, and offered an
unapprovable candidate. Only the verdict was wrong, not the attempt, so
two attempts remain and the budget still means what it says.

    cd backend
    uv run python scripts/reopen_stranded_incident.py \
        --incident-id ae20c718-55b9-4fa3-9bd9-31b78f23495e

That is the dry run: it reads the row, checks it, prints the before and
after, and writes nothing. Add --apply to commit.

The guard refuses anything that is not EXACTLY the stranded state this
bug produces, which also makes the script safe to re-run: a second
--apply finds DEGRADED / REQUEST_HEAL, does not recognize it, and stops.

No Bright Data call is made, no repair is started, and no attempt is
consumed. The only column touched beyond the two above is `evidence`,
which gains one `incident_reopened` event, plus `updated_at`, which the
row's own onupdate maintains for any write.
"""

import argparse
import sys
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
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
from app.db.models import ReliabilityIncident
from app.domain.enums import FailureClassification, IncidentStatus, RecommendedAction

# The one appender every RecallGuard lifecycle function uses. Imported
# rather than reimplemented so this script cannot write a subtly
# different evidence shape than the code that reads it.
from app.recallguard.service import _record_event

# The exact state the bug leaves behind. All four must match: this
# script recognizes one specific wrong outcome, not "an escalated
# incident" in general. An incident escalated for any other reason --
# an exhausted budget above all -- was escalated correctly and must stay
# escalated.
EXPECTED_STATUS = IncidentStatus.MANUAL_REVIEW
EXPECTED_CLASSIFICATION = FailureClassification.EXTRACTION_DRIFT
EXPECTED_ACTION = RecommendedAction.ESCALATE
EXPECTED_REPAIR_ATTEMPTS = 1

TARGET_STATUS = IncidentStatus.DEGRADED
TARGET_ACTION = RecommendedAction.REQUEST_HEAL

REOPEN_REASON = "candidate_missing_preview_escalation_bug"


def describe(incident: ReliabilityIncident) -> str:
    """The four fields the guard judges, on one line."""
    return (
        f"status={incident.status.value} "
        f"classification={incident.classification.value} "
        f"recommended_action={incident.recommended_action.value} "
        f"repair_attempts={incident.repair_attempts}"
    )


def refusal_reason(incident: ReliabilityIncident) -> str | None:
    """Why this incident is not the stranded one, or None if it is.

    Every mismatch is reported at once rather than the first, so a
    refusal says what the row actually looks like instead of sending the
    operator round the loop one field at a time.
    """
    mismatches = [
        f"{name}: expected {expected}, found {found}"
        for name, expected, found in (
            ("status", EXPECTED_STATUS.value, incident.status.value),
            (
                "classification",
                EXPECTED_CLASSIFICATION.value,
                incident.classification.value,
            ),
            (
                "recommended_action",
                EXPECTED_ACTION.value,
                incident.recommended_action.value,
            ),
            (
                "repair_attempts",
                EXPECTED_REPAIR_ATTEMPTS,
                incident.repair_attempts,
            ),
        )
        if str(expected) != str(found)
    ]
    if not mismatches:
        return None
    return "; ".join(mismatches)


def reopen(
    session: Session,
    incident: ReliabilityIncident,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Apply the reversal and its evidence in one transaction.

    Both fields and the evidence event are staged together and committed
    once, so the row can never end up reopened without a record of why,
    nor carrying a record of a reopening that did not happen. Any failure
    rolls the whole thing back.

    The caller is responsible for having checked refusal_reason first.
    """
    previous_status = incident.status
    previous_action = incident.recommended_action

    try:
        incident.status = TARGET_STATUS
        incident.recommended_action = TARGET_ACTION
        incident.evidence = _record_event(
            incident.evidence,
            {
                "event": "incident_reopened",
                "at": now().isoformat(),
                "reason": REOPEN_REASON,
                "previous_status": previous_status.value,
                "previous_action": previous_action.value,
                "new_status": TARGET_STATUS.value,
                "new_action": TARGET_ACTION.value,
                # Recorded explicitly to make it evident on the incident
                # that no attempt was refunded: two remain, not three.
                "repair_attempts": incident.repair_attempts,
            },
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    session.refresh(incident)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Return ONE incident escalated by the missing-preview preflight "
            "bug to DEGRADED / REQUEST_HEAL."
        ),
        epilog=(
            "Dry run by default: nothing is written without --apply.\n\n"
            "Refuses any incident that is not exactly "
            f"{EXPECTED_STATUS.value} / {EXPECTED_CLASSIFICATION.value} / "
            f"{EXPECTED_ACTION.value} with repair_attempts="
            f"{EXPECTED_REPAIR_ATTEMPTS}, so an incident escalated for a "
            "legitimate reason (an exhausted repair budget above all) is "
            "never reopened by it.\n\n"
            "Makes no provider calls, starts no repair, and consumes no "
            "repair attempt."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--incident-id", required=True, help="ReliabilityIncident row id (UUID)"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the change. Without this the script only reports.",
    )
    args = parser.parse_args()

    try:
        incident_id = uuid.UUID(args.incident_id)
    except ValueError:
        print(f"Not a UUID: {args.incident_id}", file=sys.stderr)
        return 2

    settings = get_settings()
    engine = create_engine(settings.DATABASE_URL)

    with Session(engine) as session:
        incident = session.get(ReliabilityIncident, incident_id)
        if incident is None:
            print(f"No incident {incident_id}", file=sys.stderr)
            return 1

        print(f"incident   {incident.id}")
        print(f"collector  {incident.collector_id}")
        print(f"before     {describe(incident)}")

        reason = refusal_reason(incident)
        if reason is not None:
            print(
                "Refusing: this is not the incident stranded by the "
                f"missing-preview bug ({reason}).",
                file=sys.stderr,
            )
            return 1

        if not args.apply:
            print(
                f"after      status={TARGET_STATUS.value} "
                f"classification={incident.classification.value} "
                f"recommended_action={TARGET_ACTION.value} "
                f"repair_attempts={incident.repair_attempts}  (planned)"
            )
            print("DRY RUN -- nothing written. Re-run with --apply to commit.")
            return 0

        reopen(session, incident)
        print(f"after      {describe(incident)}")
        print(f"APPLIED -- evidence event 'incident_reopened' ({REOPEN_REASON})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
