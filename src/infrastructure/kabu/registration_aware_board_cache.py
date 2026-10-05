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
        self._started_at = perf_counter()
        self._last_clear_at: float | None = None
        self._fetch_meta: dict[str, dict] = {}
        self._retry_meta: dict[str, dict] = {}
        self._first_fetch_at: dict[str, float] = {}
        self._last_fetch_at: float | None = None
    @property
    def requests_since_clear(self) -> int:
        return self._requests_since_clear

    @property
    def seconds_since_last_clear(self) -> float:
        """直近の登録解除からの経過秒。解除前は生成時(実行開始)からの秒。"""
        base = self._last_clear_at if self._last_clear_at is not None else self._started_at
        return perf_counter() - base

    def get_fetch_meta(self, symbol) -> dict | None:
        """実取得時の診断メタ情報。キャッシュヒット時も最初の実取得分を返す。"""
        return self._fetch_meta.get(str(symbol))

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
        self._last_clear_at = perf_counter()
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
        meta = {
            "board_fetch_seq": self.fetch_count,
            "requests_since_clear": self._requests_since_clear,
            "seconds_since_last_clear": round(self.seconds_since_last_clear, 3),
            "seconds_since_previous_fetch": (
                round(started - self._last_fetch_at, 3) if self._last_fetch_at is not None else None
            ),
        }
        self._fetch_meta[key] = meta
        self._last_fetch_at = started
        self._first_fetch_at.setdefault(key, started)
        diagnostic_getter = getattr(self.board_client, "get_current_board_for_diagnostics", None)
        try:
            board = (diagnostic_getter or self.board_client.get_current_board)(symbol)
        except Exception as exc:
            self._cache[key] = ("error", exc)
            raise
        finally:
            self._requests_since_clear += 1
            elapsed = perf_counter() - started
            meta["board_fetch_elapsed_ms"] = round(elapsed * 1000, 3)
            self.fetch_duration_ms += elapsed * 1000
        self._cache[key] = ("result", board)
        return board

    def get_retry_meta(self, symbol) -> dict | None:
        return self._retry_meta.get(str(symbol))

    def refetch_current_board(self, symbol):
        """キャッシュを通さず板を取り直す。取得できた結果はキャッシュも更新し、失敗時は既存のキャッシュを残す。"""
        key = str(symbol)
        if self._blocked_error is not None:
            raise self._blocked_error
        cleared = False
        if self._requests_since_clear >= self.batch_size:
            if not self.clear_registrations():
                raise self._blocked_error
            cleared = True

        started = perf_counter()
        self.fetch_count += 1
        first = self._first_fetch_at.get(key)
        meta = {
            "retry_fetch_seq": self.fetch_count,
            "retry_requests_since_clear": self._requests_since_clear,
            "retry_preceded_by_registration_clear": cleared,
            "retry_seconds_since_first_fetch": round(started - first, 3) if first is not None else None,
            "retry_seconds_since_previous_fetch": (
                round(started - self._last_fetch_at, 3) if self._last_fetch_at is not None else None
            ),
        }
        self._retry_meta[key] = meta
        self._last_fetch_at = started
        diagnostic_getter = getattr(self.board_client, "get_current_board_for_diagnostics", None)
        try:
            board = (diagnostic_getter or self.board_client.get_current_board)(symbol)
        finally:
            self._requests_since_clear += 1
            elapsed = perf_counter() - started
            meta["retry_elapsed_ms"] = round(elapsed * 1000, 3)
            self.fetch_duration_ms += elapsed * 1000
        if board:
            self._cache[key] = ("result", board)
        return board