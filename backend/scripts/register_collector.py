"""Register ONE collector row against an existing Source.

There is no application path that creates a Collector -- the API is
read-only for them (GET /collectors, GET /collectors/{id}/runs), the
migrations are schema-only, and nothing seeds rows. Registration has been
manual, which is fine until the row being added sits next to a collector
that a live RecallGuard incident depends on. Hence a guarded script
rather than another hand-written INSERT.

    cd backend
    uv run python scripts/register_collector.py \
        --source-id <uuid of the Source row> \
        --provider brightdata \
        --external-collector-id c_xxxxxxxxxxxxxxxx \
        --name gapradar-fix-my-itch

That is the dry run: it resolves the source, checks for a conflicting
collector, prints what would be inserted, and writes nothing. Add
--apply to commit.

What it will not do:

- Update anything. The only statement it can issue is one INSERT. An
  existing collector cannot be renamed, repointed, deactivated, or
  deleted through this script, so a demo collector sitting in the same
  table is untouchable by it.
- Insert a second row for an external_collector_id already registered.
  The database constrains (provider, external_collector_id); this refuses
  on the id alone, under any provider, because the same Bright Data
  collector registered twice is a mistake worth catching even if the
  constraint would tolerate it.
- Create a Source. A missing source is a refusal, not something to
  invent -- collectors on an invented source would collect into a
  namespace nothing else reads.
- Call Bright Data. The external id is recorded, never verified. Whether
  the collector exists at the provider is answered by running a
  collection, not by registering one.
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

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Collector, Source
from app.domain.enums import CollectorStatus

# A registered collector is one GapRadar intends to run. Anything else
# would be registered and then immediately skipped by the daily job,
# which is a state worth having a reason for -- so this script does not
# offer it.
REGISTERED_STATUS = CollectorStatus.ACTIVE


def find_conflict(session: Session, *, external_collector_id: str) -> Collector | None:
    """The collector already holding this external id, if any.

    Deliberately not scoped by provider. The database's uniqueness
    constraint is (provider, external_collector_id), but two providers
    claiming the same external id is far more likely to be a typo than a
    genuine collision, and the cost of stopping to look is nothing.
    """
    return session.execute(
        select(Collector).where(
            Collector.external_collector_id == external_collector_id
        )
    ).scalar_one_or_none()


def register(
    session: Session,
    *,
    source: Source,
    provider: str,
    external_collector_id: str,
    name: str,
) -> Collector:
    """Insert the collector. One statement, one commit.

    The caller is responsible for having checked find_conflict first.
    """
    collector = Collector(
        source_id=source.id,
        provider=provider,
        external_collector_id=external_collector_id,
        name=name,
        status=REGISTERED_STATUS,
    )
    session.add(collector)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(collector)
    return collector


def describe(collector: Collector) -> str:
    return (
        f"id={collector.id} source_id={collector.source_id} "
        f"provider={collector.provider} "
        f"external_collector_id={collector.external_collector_id} "
        f"name={collector.name!r} status={collector.status.value}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register one Collector row against an existing Source.",
        epilog=(
            "Dry run by default: nothing is written without --apply.\n\n"
            "Only ever inserts. No existing collector is read for update, "
            "modified, or deleted, and no Bright Data call is made."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--source-id", required=True, help="Source row id (UUID)")
    parser.add_argument(
        "--provider", required=True, help="Provider namespace, e.g. brightdata"
    )
    parser.add_argument(
        "--external-collector-id",
        required=True,
        help="The provider's own collector id, e.g. c_xxxxxxxxxxxxxxxx",
    )
    parser.add_argument("--name", required=True, help="Human-readable collector name")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the insert. Without this the script only reports.",
    )
    args = parser.parse_args()

    try:
        source_id = uuid.UUID(args.source_id)
    except ValueError:
        print(f"Not a UUID: {args.source_id}", file=sys.stderr)
        return 2

    settings = get_settings()
    engine = create_engine(settings.DATABASE_URL)

    with Session(engine) as session:
        source = session.get(Source, source_id)
        if source is None:
            print(
                f"No source {source_id}. Registering a collector against a "
                "source that does not exist would collect into a namespace "
                "nothing reads.",
                file=sys.stderr,
            )
            return 1

        print(f"source     {source.id} {source.name!r} active={source.active}")
        print(f"           {source.base_url}")

        conflict = find_conflict(
            session, external_collector_id=args.external_collector_id
        )
        if conflict is not None:
            print(
                "Refusing: external_collector_id "
                f"{args.external_collector_id!r} is already registered as "
                f"{describe(conflict)}",
                file=sys.stderr,
            )
            return 1

        planned = (
            f"source_id={source.id} provider={args.provider} "
            f"external_collector_id={args.external_collector_id} "
            f"name={args.name!r} status={REGISTERED_STATUS.value}"
        )

        if not args.apply:
            print(f"insert     {planned}  (planned)")
            print("DRY RUN -- nothing written. Re-run with --apply to commit.")
            return 0

        collector = register(
            session,
            source=source,
            provider=args.provider,
            external_collector_id=args.external_collector_id,
            name=args.name,
        )
        print(f"inserted   {describe(collector)}")
        print("APPLIED -- no existing collector was read for update or modified.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
