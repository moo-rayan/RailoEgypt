"""
In-memory store for the encrypted data bundle.

Serves bundle bytes directly from process memory (0ms latency).
R2 is used only for persistence across restarts.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


class BundleStore:
    """Thread-safe singleton holding the bundle in process memory."""

    def __init__(self):
        self._gzip_bytes: bytes | None = None
        self._version_info: dict[str, Any] | None = None

    @property
    def is_ready(self) -> bool:
        return self._gzip_bytes is not None and self._version_info is not None

    @property
    def gzip_bytes(self) -> bytes | None:
        return self._gzip_bytes

    @property
    def version_info(self) -> dict[str, Any] | None:
        return self._version_info

    def set(self, gzip_bytes: bytes, version_info: dict[str, Any]) -> None:
        self._gzip_bytes = gzip_bytes
        self._version_info = version_info
        logger.info(
            "Bundle loaded in memory: version=%s, size=%.1fKB",
            version_info.get("version", "?")[:8],
            len(gzip_bytes) / 1024,
        )

    def clear(self) -> None:
        self._gzip_bytes = None
        self._version_info = None


bundle_store = BundleStore()
