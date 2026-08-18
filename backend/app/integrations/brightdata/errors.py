class BrightDataError(Exception):
    """Base class for all Bright Data integration errors.

    Never include the API token or any other credential in the message of
    this exception or its subclasses.
    """


class BrightDataAuthenticationError(BrightDataError):
    """Raised when Bright Data rejects the API token (401/403)."""


class BrightDataTimeoutError(BrightDataError):
    """Raised when a request to Bright Data exceeds the configured timeout."""


class BrightDataProviderUnavailableError(BrightDataError):
    """Raised on connection failures or a 5xx response from Bright Data."""


class BrightDataNotFoundError(BrightDataError):
    """Raised when Bright Data answers 404 for a resource.

    Kept apart from the generic 4xx error because "this collector has no
    self-healing job" and "the provider rejected the request" lead to
    opposite decisions: the first means there is no repair to resume and
    a new one may safely be triggered, while the second means we do not
    know what is happening and must not trigger anything.
    """


class BrightDataInvalidResponseError(BrightDataError):
    """Raised when a response is not valid JSON or is missing/has
    unexpected required fields.
    """


class BrightDataUnverifiedCapabilityError(BrightDataError):
    """Raised by client methods whose Bright Data HTTP contract has not
    been confirmed against official documentation.

    These methods intentionally do not perform a network call. Fabricating
    a request/response shape for an unverified endpoint would be worse
    than refusing to act, since it could silently succeed against the
    wrong contract or silently misparse a real response.
    """


class BrightDataMalformedDatasetError(BrightDataInvalidResponseError):
    """Raised when a completed dataset contains a row that is not an object.

    A dataset row that is a string, null, number, or array is not a
    record this pipeline can reason about. Dropping such a row would
    quietly shrink the dataset and make "the scraper returned fewer
    rows" indistinguishable from "the scraper returned garbage", so the
    whole response is rejected instead.

    The offending row's index and Python type name are preserved. The
    value itself is deliberately not embedded: it is untrusted provider
    content, and the index is enough to find it in the raw response.
    """

    def __init__(self, message: str, *, index: int, value_type: str) -> None:
        self.index = index
        self.value_type = value_type
        super().__init__(message)
