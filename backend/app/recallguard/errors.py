"""RecallGuard domain errors.

Guard rails, not control flow: each one marks an operation RecallGuard
refuses to perform because performing it would let something unproven
pass as proven.
"""


class RecallGuardError(Exception):
    """Base class for RecallGuard domain errors."""


class NonTerminalRunError(RecallGuardError):
    """A collector run was evaluated before it finished executing."""


class IncidentTransitionError(RecallGuardError):
    """A lifecycle transition was refused.

    The incident is left exactly as it was: a rejected transition never
    half-advances an incident.
    """


class RepairAttemptLimitExceededError(RecallGuardError):
    """A fourth autonomous repair cycle was requested.

    The incident is moved to MANUAL_REVIEW / ESCALATE instead. Repeatedly
    re-running an autonomous repair that has already failed three times
    is not reliability engineering, it is a loop.
    """


class VerificationRunRejectedError(RecallGuardError):
    """A run was offered as recovery proof and did not qualify.

    Recovery requires a fresh, independent collection: the same run that
    detected the failure, a run from another collector, a run that
    predates the repair, or a run already used to verify this incident
    can never establish it.
    """
