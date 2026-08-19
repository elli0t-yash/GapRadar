"""Failures of the semantic judge, kept apart from what they are not.

A judgement that could not be obtained is NOT evidence that a paper is
irrelevant. Every error here means "we do not know", and the adapter
turns them all into a declined verdict rather than a low score.
"""


class SemanticJudgeError(Exception):
    """Base class. Never carries an API key or any other credential."""


class SemanticJudgeUnavailableError(SemanticJudgeError):
    """No credential is configured, so no judgement can be attempted.

    Raised at construction rather than per call: a matcher that cannot
    possibly work should fail when it is built, not silently decline
    every paper and look like a very harsh judge.
    """


class SemanticJudgeTransportError(SemanticJudgeError):
    """The provider could not be reached, timed out, or returned 5xx/429."""


class SemanticJudgeResponseError(SemanticJudgeError):
    """The provider answered, but the answer is not a usable verdict.

    Malformed output is a MATCHER failure. It must never be recorded as
    relevance 0 -- that would turn "the judge malfunctioned" into "the
    research is irrelevant", which is a different and unearned claim.
    """
