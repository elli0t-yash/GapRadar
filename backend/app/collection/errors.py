"""Failure taxonomy for one orchestrated collection run.

Every error here describes *why this orchestration run did not produce
ingested signals*. None of them is a trust verdict: whether downstream
systems should trust a source, a collector, or a dataset is RecallGuard's
concern and does not exist yet.

`stage` is a stable machine-readable label for where the run stopped. It
is persisted on the CollectorRun for later diagnosis and never used to
drive control flow.

None of these messages may ever contain the Bright Data API token or an
Authorization header -- the transport layer (BrightDataClient) already
guarantees that for the errors it raises, and nothing here reintroduces
credentials into a message.
"""

import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.integrations.brightdata.fix_my_itch import FixMyItchDatasetReport


class CollectionError(Exception):
    """Base class for orchestration failures.

    Carries the CollectorRun this failure belongs to when one exists.
    `collector_run_id` is None only for a trigger failure, where no run
    row can be created (see app.collection.service for why).
    """

    stage = "unknown"

    def __init__(
        self, message: str, *, collector_run_id: uuid.UUID | None = None
    ) -> None:
        self.collector_run_id = collector_run_id
        super().__init__(message)

    def evidence(self) -> dict[str, Any]:
        """Structured detail persisted on the run for later diagnosis."""
        return {}


class CollectionTriggerError(CollectionError):
    """Bright Data rejected or failed the trigger request."""

    stage = "trigger"


class CollectionExecutionError(CollectionError):
    """Bright Data failed while running or serving the collection."""

    stage = "collection"


class CollectionTimeoutError(CollectionError):
    """The LOCAL orchestration budget elapsed before the collection finished.

    This is GapRadar's own patience running out, not Bright Data's. It is
    never sent to Bright Data as a `deadline` parameter: doing so once
    terminated a real production run mid-collection.
    """

    stage = "timeout"

    def __init__(
        self,
        message: str,
        *,
        collector_run_id: uuid.UUID | None = None,
        timeout_seconds: float,
        polls: int,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.polls = polls
        super().__init__(message, collector_run_id=collector_run_id)

    def evidence(self) -> dict[str, Any]:
        return {"timeout_seconds": self.timeout_seconds, "polls": self.polls}


class MalformedCollectionPayloadError(CollectionError):
    """Bright Data returned a payload that is not a usable dataset.

    Raised instead of degrading a malformed response into an empty
    dataset -- "the scraper returned nothing" and "we could not read the
    response" are different facts and must stay distinguishable.
    """

    stage = "payload"


class SourceContractValidationError(CollectionError):
    """At least one record violated the source's own contract.

    Fail-closed: not one record of the dataset is ingested. The full
    report of invalid records is preserved as evidence.
    """

    stage = "source_validation"

    def __init__(
        self,
        message: str,
        *,
        collector_run_id: uuid.UUID | None = None,
        report: "FixMyItchDatasetReport",
        fetched_record_count: int,
    ) -> None:
        self.report = report
        self.fetched_record_count = fetched_record_count
        super().__init__(message, collector_run_id=collector_run_id)

    def evidence(self) -> dict[str, Any]:
        return {
            "fetched_record_count": self.fetched_record_count,
            "valid_record_count": len(self.report.valid),
            "invalid_record_count": len(self.report.invalid),
            "source_duplicate_count": len(self.report.duplicates),
            "invalid_records": [
                invalid.model_dump(mode="json") for invalid in self.report.invalid
            ],
        }


class CollectionIngestionError(CollectionError):
    """Persisting the validated dataset failed.

    Also raised when the generic ingestion pipeline rejects a record that
    already passed source validation: that combination means the source
    adapter and the ingestion contract have drifted apart, and the run
    must not be reported as successful.
    """

    stage = "ingestion"

    def __init__(
        self,
        message: str,
        *,
        collector_run_id: uuid.UUID | None = None,
        rejected: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rejected = rejected or []
        super().__init__(message, collector_run_id=collector_run_id)

    def evidence(self) -> dict[str, Any]:
        return {"rejected_records": self.rejected}
