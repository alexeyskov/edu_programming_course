from __future__ import annotations


class IntegrationError(RuntimeError):
    """Safe-to-log base error which never embeds credentials or response bodies."""

    code = "INTEGRATION_ERROR"

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class IntegrationConfigurationError(IntegrationError):
    code = "NOT_CONFIGURED"


class IntegrationTimeout(IntegrationError):
    code = "TIMEOUT"

    def __init__(self, message: str = "External service timed out") -> None:
        super().__init__(message, retryable=True)


class IntegrationUnavailable(IntegrationError):
    code = "UNAVAILABLE"

    def __init__(self, message: str = "External service is unavailable") -> None:
        super().__init__(message, retryable=True)


class IntegrationBusy(IntegrationUnavailable):
    code = "BROWSER_BUSY"

    def __init__(self, message: str = "External service is busy") -> None:
        super().__init__(message)


class IntegrationAttemptFinalized(IntegrationError):
    """The LMS proved that the student's attempt is already terminal.

    This is a non-retryable state transition rather than a transport failure:
    retrying the same checkpoint could overwrite a response completed directly
    in the LMS.
    """

    code = "MOODLE_ATTEMPT_FINALIZED"


class IntegrationAssessmentUnavailable(IntegrationError):
    """The LMS proved that this student cannot currently open the work."""

    code = "MOODLE_ASSESSMENT_UNAVAILABLE"


class IntegrationProtocolError(IntegrationError):
    code = "INVALID_RESPONSE"


class IntegrationResponseTooLarge(IntegrationProtocolError):
    code = "RESPONSE_TOO_LARGE"
