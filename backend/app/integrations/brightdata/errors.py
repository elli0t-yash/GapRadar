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
