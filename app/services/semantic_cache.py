"""
Semantic Cache for Chat Responses using Redis.

Uses simple text similarity (normalized Levenshtein) to match questions.
Stores: question_hash → {question, response, timestamp, usage_count}
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from difflib import SequenceMatcher
from typing import Any

from app.core.cache import get_redis

logger = logging.getLogger(__name__)

# Cache config
CACHE_TTL = 3600  # 1 hour
SIMILARITY_THRESHOLD = 0.85  # 85% match required
MAX_CACHE_ENTRIES = 1000  # Prevent unbounded growth


class SemanticCache:
    """Redis-backed semantic cache for chat responses."""

    def __init__(self) -> None:
        self._prefix = "chat:semantic:"
        self._index_key = "chat:semantic:index"  # Set of all cached question hashes

    def _normalize_text(self, text: str) -> str:
        """Normalize text for better matching."""
        text = text.lower().strip()
        # Remove extra whitespace
        text = " ".join(text.split())
        # Remove common punctuation that doesn't change meaning
        for char in "؟?!.،,":
            text = text.replace(char, "")
        return text

    def _text_similarity(self, text1: str, text2: str) -> float:
        """Calculate similarity between two texts (0.0 to 1.0)."""
        norm1 = self._normalize_text(text1)
        norm2 = self._normalize_text(text2)
        return SequenceMatcher(None, norm1, norm2).ratio()

    def _make_hash(self, text: str) -> str:
        """Create hash from normalized text."""
        normalized = self._normalize_text(text)
        return hashlib.md5(normalized.encode()).hexdigest()[:16]

    async def get(self, question: str) -> dict[str, Any] | None:
        """
        Get cached response for similar question.
        Returns None if no match found above threshold.
        """
        redis = await get_redis()
        if redis is None:
            return None

        try:
            # Get all cached question hashes
            cached_hashes = await redis.smembers(self._index_key)
            if not cached_hashes:
                return None

            best_match: dict[str, Any] | None = None
            best_similarity = 0.0

            # Check each cached question for similarity
            for q_hash in cached_hashes:
                key = f"{self._prefix}{q_hash}"
                cached_data = await redis.get(key)
                if not cached_data:
                    # Expired or deleted, remove from index
                    await redis.srem(self._index_key, q_hash)
                    continue

                cached = json.loads(cached_data)
                similarity = self._text_similarity(question, cached["question"])

                if similarity > best_similarity:
                    best_similarity = similarity
                    best_match = cached

            # Return if above threshold
            if best_match and best_similarity >= SIMILARITY_THRESHOLD:
                # Increment usage counter
                q_hash = self._make_hash(best_match["question"])
                best_match["usage_count"] = best_match.get("usage_count", 0) + 1
                best_match["last_used"] = time.time()
                key = f"{self._prefix}{q_hash}"
                await redis.setex(key, CACHE_TTL, json.dumps(best_match, ensure_ascii=False))

                logger.info(
                    "Semantic cache HIT (%.1f%% similar): %s",
                    best_similarity * 100,
                    question[:50],
                )
                return {
                    "reply": best_match["response"],
                    "tool_used": best_match.get("tool_used"),
                    "tool_data": best_match.get("tool_data"),
                    "provider": best_match.get("provider"),
                    "cached": True,
                    "similarity": best_similarity,
                }

            logger.debug("Semantic cache MISS (best: %.1f%%)", best_similarity * 100)
            return None

        except Exception as e:
            logger.error("Semantic cache get error: %s", e)
            return None

    async def set(
        self,
        question: str,
        response: str,
        tool_used: str | None = None,
        tool_data: dict | None = None,
        provider: str | None = None,
    ) -> None:
        """Cache a question-response pair."""
        redis = await get_redis()
        if redis is None:
            return

        try:
            # Evict old entries if cache is too large
            index_size = await redis.scard(self._index_key)
            if index_size >= MAX_CACHE_ENTRIES:
                # Remove 10% oldest entries (simple LRU)
                await self._evict_old_entries(int(MAX_CACHE_ENTRIES * 0.1))

            q_hash = self._make_hash(question)
            key = f"{self._prefix}{q_hash}"

            cache_entry = {
                "question": question,
                "response": response,
                "tool_used": tool_used,
                "tool_data": tool_data,
                "provider": provider,
                "timestamp": time.time(),
                "usage_count": 1,
                "last_used": time.time(),
            }

            await redis.setex(key, CACHE_TTL, json.dumps(cache_entry, ensure_ascii=False))
            await redis.sadd(self._index_key, q_hash)

            logger.debug("Semantic cache SET: %s", question[:50])

        except Exception as e:
            logger.error("Semantic cache set error: %s", e)

    async def _evict_old_entries(self, count: int) -> None:
        """Evict oldest entries from cache."""
        redis = await get_redis()
        if redis is None:
            return

        try:
            cached_hashes = await redis.smembers(self._index_key)
            entries_with_time: list[tuple[str, float]] = []

            for q_hash in cached_hashes:
                key = f"{self._prefix}{q_hash}"
                cached_data = await redis.get(key)
                if cached_data:
                    cached = json.loads(cached_data)
                    last_used = cached.get("last_used", cached.get("timestamp", 0))
                    entries_with_time.append((q_hash, last_used))

            # Sort by last_used (oldest first)
            entries_with_time.sort(key=lambda x: x[1])

            # Remove oldest N entries
            for q_hash, _ in entries_with_time[:count]:
                key = f"{self._prefix}{q_hash}"
                await redis.delete(key)
                await redis.srem(self._index_key, q_hash)

            logger.info("Evicted %d old semantic cache entries", count)

        except Exception as e:
            logger.error("Semantic cache eviction error: %s", e)

    async def clear(self) -> None:
        """Clear all cached entries."""
        redis = await get_redis()
        if redis is None:
            return

        try:
            cached_hashes = await redis.smembers(self._index_key)
            for q_hash in cached_hashes:
                key = f"{self._prefix}{q_hash}"
                await redis.delete(key)
            await redis.delete(self._index_key)
            logger.info("Semantic cache cleared")
        except Exception as e:
            logger.error("Semantic cache clear error: %s", e)


# Singleton
_cache: SemanticCache | None = None


def get_semantic_cache() -> SemanticCache:
    global _cache
    if _cache is None:
        _cache = SemanticCache()
    return _cache
