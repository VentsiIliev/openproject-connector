import threading
import time


class CacheEntry:
    def __init__(self, value, ttl: int):
        self.value = value
        self.expires_at = time.monotonic() + ttl


class TTLCache:
    def __init__(self, max_size: int = 128, default_ttl: int = 300):
        self._store: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self.max_size = max_size
        self.default_ttl = default_ttl

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if time.monotonic() > entry.expires_at:
                self._store.pop(key, None)
                return None
            return entry.value

    def set(self, key: str, value, ttl: int | None = None):
        with self._lock:
            if len(self._store) >= self.max_size:
                self._evict_expired()
                if len(self._store) >= self.max_size:
                    oldest_key = next(iter(self._store))
                    self._store.pop(oldest_key)
            self._store[key] = CacheEntry(value, ttl or self.default_ttl)

    def invalidate(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def clear(self):
        with self._lock:
            self._store.clear()

    def _evict_expired(self):
        now = time.monotonic()
        expired = [k for k, v in self._store.items() if now > v.expires_at]
        for k in expired:
            self._store.pop(k, None)


cache = TTLCache(max_size=256, default_ttl=300)
