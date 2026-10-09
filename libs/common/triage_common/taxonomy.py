SEVERITIES = ("P1", "P2", "P3", "P4")

CATEGORIES = (
    "DATABASE_CONNECTIVITY",
    "NETWORK_CONNECTIVITY",
    "RESOURCE_EXHAUSTION",
    "AUTHENTICATION_AUTHORIZATION",
    "CERTIFICATE_EXPIRY",
    "DEPENDENCY_FAILURE",
    "BATCH_JOB_FAILURE",
    "APPLICATION_ERROR",
    "DATA_QUALITY",
    "CONFIGURATION_ERROR",
    "UNCLASSIFIED",
)


def severity_rank(severity: str) -> int:
    """Lower rank = more severe (P1 -> 1)."""
    return SEVERITIES.index(severity) + 1
