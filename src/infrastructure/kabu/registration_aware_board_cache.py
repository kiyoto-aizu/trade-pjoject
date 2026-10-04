from time import perf_counter


class RegistrationAwareBoardCache:
    """板取得を同日中に共有し、API登録銘柄数を50以下に保ちます。"""

    def __init__(self, board_client, unregister_callback, batch_size: int = 50):
        self.board_client = board_client
        self.unregister_callback = unregister_callback
        self.batch_size = batch_size
        self._cache: dict[str, tuple[str, object]] = {}
        self._requests_since_clear = 0
        self._blocked_error: Exception | None = None
        self.fetch_count = 0
        self.cache_hit_count = 0
        self.fetch_duration_ms = 0.0
        self.unregister_count = 0

    def clear_registrations(self) -> bool:
        self.unregister_count += 1
        try:
            result = self.unregister_callback()
        except Exception as exc:
            self._blocked_error = RuntimeError("銘柄登録解除に失敗しました")
            self._blocked_error.__cause__ = exc
            return False
        if result is None:
            self._blocked_error = RuntimeError("銘柄登録解除に失敗しました")
            return False
        self._requests_since_clear = 0
        self._blocked_error = None
        return True

    def get_current_board(self, symbol):
        key = str(symbol)
        saved = self._cache.get(key)
        if saved is not None:
            self.cache_hit_count += 1
            if saved[0] == "error":
                raise saved[1]
            return saved[1]
        if self._blocked_error is not None:
            raise self._blocked_error
        if self._requests_since_clear >= self.batch_size:
            if not self.clear_registrations():
                raise self._blocked_error

        started = perf_counter()
        self.fetch_count += 1
        try:
            board = self.board_client.get_current_board(symbol)
        except Exception as exc:
            self._cache[key] = ("error", exc)
            raise
        finally:
            self._requests_since_clear += 1
            self.fetch_duration_ms += (perf_counter() - started) * 1000
        self._cache[key] = ("result", board)
        return board