"""
ui_blueprint.domain.store
=========================
In-memory storage for DomainProfile objects.

The DomainProfileStore interface provides a clear boundary so the
backing storage can be swapped for a database later without changing
callers.
"""

from __future__ import annotations

import abc
from typing import Any

from ui_blueprint.domain.ir import DomainProfile


class DomainProfileStore(abc.ABC):
    """Abstract storage interface for DomainProfile objects."""

    @abc.abstractmethod
    def save(self, profile: DomainProfile) -> DomainProfile:
        """Persist (insert or update) a profile and return it."""

    @abc.abstractmethod
    def get(self, profile_id: str) -> DomainProfile | None:
        """Return the profile for *profile_id*, or None if not found."""

    @abc.abstractmethod
    def list_all(self) -> list[DomainProfile]:
        """Return all stored profiles."""


class InMemoryDomainProfileStore(DomainProfileStore):
    """
    Thread-unsafe in-memory implementation of DomainProfileStore.

    Suitable for single-process use during development and tests.
    Replace with a persistent store backed by SQLite or a hosted DB for
    production.
    """

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def save(self, profile: DomainProfile) -> DomainProfile:
        self._store[profile.id] = profile.to_dict()
        return profile

    def get(self, profile_id: str) -> DomainProfile | None:
        data = self._store.get(profile_id)
        if data is None:
            return None
        return DomainProfile.from_dict(data)

    def list_all(self) -> list[DomainProfile]:
        return [DomainProfile.from_dict(d) for d in self._store.values()]
