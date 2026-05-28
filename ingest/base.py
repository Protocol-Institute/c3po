"""Base interface for all corpus ingest sources."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class IngestResult:
    source_id: str
    dry_run: bool
    vectors_upserted: int = 0
    vectors_deleted: int = 0
    items_processed: int = 0
    errors: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseSource(ABC):
    """
    Abstract base for all corpus sources.

    Each concrete source corresponds to one entry in config/source_registry.json.
    For Phase 1 the concrete subclasses are thin subprocess wrappers around the
    existing ingest scripts. They will be replaced with direct implementations
    as each script is refactored.
    """

    source_id: str    # matches key in config/source_registry.json
    namespace: str    # Pinecone namespace
    ownership: str    # owned | subscribed | aware

    @abstractmethod
    def run(self, dry_run: bool = False, **kwargs) -> IngestResult:
        """Run the full ingest cycle for this source."""
        ...

    def status(self) -> dict[str, Any]:
        """Return current ingest state (last_ingested, vector_count, etc.)."""
        return {}

    def supports_incremental(self) -> bool:
        """Whether this source supports incremental sync vs. full re-ingest."""
        return False
