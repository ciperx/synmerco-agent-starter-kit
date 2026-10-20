"""Synmerco agent starter kit."""

from .client import (
    Agent,
    AuthError,
    Bid,
    Job,
    PreconditionError,
    RateLimited,
    SynmercoClient,
    SynmercoError,
)

__all__ = [
    "Agent",
    "AuthError",
    "Bid",
    "Job",
    "PreconditionError",
    "RateLimited",
    "SynmercoClient",
    "SynmercoError",
]
__version__ = "0.1.0"
