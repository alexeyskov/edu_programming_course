"""Framework-neutral, async transports for external systems.

The adapters in this package never open database sessions.  Callers must persist
state before/after awaiting them instead of keeping a transaction or row lock
open across a network request.
"""

from .ai import AIAnswer, AIProvider
from .authorship import AuthorshipResult, AuthorshipTransport
from .errors import (
    IntegrationConfigurationError,
    IntegrationError,
    IntegrationProtocolError,
    IntegrationResponseTooLarge,
    IntegrationTimeout,
    IntegrationUnavailable,
)
from .moodle import CourseDiscovery, MoodleBridge
from .moodle_standard import (
    MoodleAuthenticationError,
    MoodleCourseMembership,
    MoodleStandardClient,
    MoodleWebServicesDisabled,
    TokenIdentity,
)
from .runner import RunnerAdapter, RunnerResult

__all__ = [
    "AIAnswer",
    "AIProvider",
    "AuthorshipResult",
    "AuthorshipTransport",
    "CourseDiscovery",
    "IntegrationConfigurationError",
    "IntegrationError",
    "IntegrationProtocolError",
    "IntegrationResponseTooLarge",
    "IntegrationTimeout",
    "IntegrationUnavailable",
    "MoodleBridge",
    "MoodleAuthenticationError",
    "MoodleCourseMembership",
    "MoodleStandardClient",
    "MoodleWebServicesDisabled",
    "RunnerAdapter",
    "RunnerResult",
    "TokenIdentity",
]
