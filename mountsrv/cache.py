"""In-memory LRU cache with TTL for directory listings"""

import time
import logging
from typing import Any, Dict
from collections import OrderedDict

logger = logging.getLogger(__name__)


class LRUCache:
    """Simple in-memory LRU cache with TTL"""

    def __init__(self, max_size: int = 5000, ttl: int = 600):
        """
        Args:
            max_size: Maximum number of entries (default: 5000)
            ttl: Time-to-live in seconds (default: 600s = 10 minutes)
        """
        self.max_size = max_size
        self.ttl = ttl
        self._cache: OrderedDict = OrderedDict()
        self._timestamps: Dict[str, float] = {}

    def get(self, key: str) -> Any:
        """Get value from cache if exists and not expired"""
        if key not in self._cache:
            return None

        # Check TTL
        if time.time() - self._timestamps[key] > self.ttl:
            self.remove(key)
            return None

        # Move to end (most recently used)
        value = self._cache.pop(key)
        self._cache[key] = value
        return value

    def set(self, key: str, value: Any) -> None:
        """Set value in cache"""
        # Remove if exists (to update position)
        if key in self._cache:
            self.remove(key)

        # Evict oldest if at capacity
        if len(self._cache) >= self.max_size:
            oldest_key = next(iter(self._cache))
            self.remove(oldest_key)

        # Add new entry
        self._cache[key] = value
        self._timestamps[key] = time.time()

    def remove(self, key: str) -> None:
        """Remove entry from cache"""
        self._cache.pop(key, None)
        self._timestamps.pop(key, None)

    def clear(self) -> None:
        """Clear all entries"""
        self._cache.clear()
        self._timestamps.clear()

    def size(self) -> int:
        """Get current cache size"""
        return len(self._cache)

    def stats(self) -> Dict[str, Any]:
        """Get cache statistics"""
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "ttl": self.ttl,
        }