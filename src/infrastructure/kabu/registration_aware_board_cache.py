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
        self._fetch_meta[key] = {
            "board_fetch_seq": self.fetch_count,
            "requests_since_clear": self._requests_since_clear,
            "seconds_since_last_clear": round(self.seconds_since_last_clear, 3),
        }
        diagnostic_getter = getattr(self.board_client, "get_current_board_for_diagnostics", None)
        try:
            board = (diagnostic_getter or self.board_client.get_current_board)(symbol)
        except Exception as exc:
            self._cache[key] = ("error", exc)
            raise
        finally:
            self._requests_since_clear += 1
            self.fetch_duration_ms += (perf_counter() - started) * 1000
        self._cache[key] = ("result", board)
        return board
