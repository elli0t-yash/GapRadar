"""Register ONE source row.

Companion to scripts/register_collector.py, for the same reason: nothing
in the application creates a Source. The API never writes one, the
migrations are schema-only, and nothing seeds rows.

    cd backend
    uv run python scripts/register_source.py \
        --name "Fix My Itch Production" \
        --source-type web \
        --base-url https://razorpay.com/m/fix-my-itch/

That is the dry run: it checks for an exact duplicate, reports any source
already pointing at the same URL, prints what would be inserted, and
writes nothing. Add --apply to commit.

Two sources may share a base_url, deliberately. The `sources` table
constrains nothing but its primary key -- `ix_sources_name` is a plain
index, not a unique one -- and signal identity is keyed on
(source_id, external_id). So a second Source over the same URL is how two
collectors scrape the same site without one's records being skipped as
duplicates of the other's. That is a real isolation boundary, not a
workaround, which is why this script reports same-URL sources rather than
refusing them: sharing a URL is sometimes exactly the intent, and the
operator is the one who knows.

What it will not do:

- Update anything. One INSERT is the only statement it can issue, so an
  existing source cannot be renamed, repointed, or deactivated through
  it.
- Insert an exact duplicate: same name AND same base_url. That pair is
  never deliberate -- it is a re-run of this script.
- Register a collector. That is scripts/register_collector.py, run
  afterwards with the id this one prints.
- Call Bright Data. A Source is a GapRadar record; the provider has no
  opinion about it.
"""

import argparse
import sys
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
from app.db.models import Source
from app.domain.enums import SourceType

# A registered source is one GapRadar intends to collect from. An
# inactive one would be registered and then immediately skipped by the
# daily job (which requires Source.active), so this script does not offer
# that state -- deactivating later is a deliberate act, not a default.
REGISTERED_ACTIVE = True


def find_duplicate(session: Session, *, name: str, base_url: str) -> Source | None:
    """The source that is this one already, if it exists.

    Both fields must match. Name alone is not enough (two sites can share
    a name) and base_url alone is emphatically not enough -- separating
    two collectors over one URL is the reason this script exists.
    """
    return session.execute(
        select(Source).where(Source.name == name, Source.base_url == base_url)
    ).scalar_one_or_none()


def find_same_url(session: Session, *, base_url: str) -> list[Source]:
    """Sources already pointing at this URL. Reported, never a refusal."""
    return list(
        session.execute(
            select(Source).where(Source.base_url == base_url).order_by(Source.name)
        ).scalars()
    )


def register(
    session: Session, *, name: str, source_type: SourceType, base_url: str
) -> Source:
    """Insert the source. One statement, one commit.

    The caller is responsible for having checked find_duplicate first.
    """
    source = Source(
        name=name,
        source_type=source_type,
        base_url=base_url,
        active=REGISTERED_ACTIVE,
    )
    session.add(source)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(source)
    return source


def describe(source: Source) -> str:
    return (
        f"id={source.id} name={source.name!r} "
        f"source_type={source.source_type.value} "
        f"base_url={source.base_url} active={source.active}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Register one Source row.",
        epilog=(
            "Dry run by default: nothing is written without --apply.\n\n"
            "Only ever inserts. No existing source is read for update, "
            "modified, or deleted, and no Bright Data call is made.\n\n"
            "A second source over an existing base_url is allowed and is "
            "reported, not refused -- it is how two collectors scrape one "
            "site without sharing signal identity."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--name", required=True, help="Human-readable source name")
    parser.add_argument(
        "--source-type",
        required=True,
        choices=[member.value for member in SourceType],
        help="Source type",
    )
    parser.add_argument("--base-url", required=True, help="Canonical source URL")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the insert. Without this the script only reports.",
    )
    args = parser.parse_args()

    source_type = SourceType(args.source_type)

    settings = get_settings()
    engine = create_engine(settings.DATABASE_URL)

    with Session(engine) as session:
        duplicate = find_duplicate(session, name=args.name, base_url=args.base_url)
        if duplicate is not None:
            print(
                "Refusing: a source with this exact name and base_url already "
                f"exists as {describe(duplicate)}",
                file=sys.stderr,
            )
            return 1

        existing = find_same_url(session, base_url=args.base_url)
        for source in existing:
            # Not a problem, but never a surprise either: registering a
            # second source over one URL is a decision, so it is stated.
            print(f"same URL   {describe(source)}")
        if existing:
            print(
                f"           ^ {len(existing)} existing source(s) share this "
                "base_url; signal identity is (source_id, external_id), so a "
                "new source collects independently of them."
            )

        planned = (
            f"name={args.name!r} source_type={source_type.value} "
            f"base_url={args.base_url} active={REGISTERED_ACTIVE}"
        )

        if not args.apply:
            print(f"insert     {planned}  (planned)")
            print("DRY RUN -- nothing written. Re-run with --apply to commit.")
            return 0

        source = register(
            session, name=args.name, source_type=source_type, base_url=args.base_url
        )
        print(f"inserted   {describe(source)}")
        print("APPLIED -- no existing source was read for update or modified.")
        print(
            "Next: register the collector against this source with\n"
            f"  uv run python scripts/register_collector.py --source-id {source.id} \\\n"
            "      --provider brightdata --external-collector-id <c_...> --name <name>"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
