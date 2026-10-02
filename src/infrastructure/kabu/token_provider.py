"""
================================================================================
kabuステーションAPIトークン提供者
複数のAPIクライアントが同じトークンを参照できるよう一元管理し、
401応答時の再取得（間隔制御・復旧失敗時のクールダウン）を調停します。
================================================================================
"""
import logging
import threading
import time

from src.config import config
from src.infrastructure.kabu.get_token import get_api_token

logger = logging.getLogger(__name__)


class TokenProvider:
    """kabuステーションAPIのトークンを保持し、401発生時の再取得を調停する。"""

    def __init__(self, fetch_token=get_api_token):
        self._fetch_token = fetch_token
        self._lock = threading.Lock()
        self._token: str | None = None
        self._last_attempt_at: float | None = None
        self._cooldown_until: float | None = None
        self._recovery_failed = False

    def get_token(self) -> str | None:
        """保持中のトークンを返す。未取得の場合は新規に取得する。"""
        with self._lock:
            if self._token is None:
                self._fetch_locked()
            return self._token

    def set_token(self, token: str | None) -> None:
        """起動時に取得済みのトークンを共有状態として登録する。"""
        with self._lock:
            self._token = token
            self._cooldown_until = None

    def recover_from_unauthorized(self) -> str | None:
        """401応答を受けて呼び出す。間隔制御のうえで新しいトークンを返す。

        再取得を抑止する場合、または再取得自体に失敗した場合はNoneを返す。
        """
        with self._lock:
            now = time.monotonic()
            if self._cooldown_until is not None and now < self._cooldown_until:
                logger.warning(
                    "401応答を受信しましたが、復旧失敗後のクールダウン中のためトークン再取得を抑止します。"
                )
                return None
            if (
                self._last_attempt_at is not None
                and now - self._last_attempt_at < config.KABU_TOKEN_REFRESH_MIN_INTERVAL_SECONDS
            ):
                logger.info("直近で再取得済みのトークンを使って再試行します（再取得は抑止）。")
                return self._token
            return self._fetch_locked()

    def report_retry_still_unauthorized(self) -> None:
        """再取得後の再試行も401だった場合に呼び出す。復旧失敗としてクールダウンへ入る。"""
        with self._lock:
            self._cooldown_until = time.monotonic() + config.KABU_TOKEN_REFRESH_FAILURE_BACKOFF_SECONDS
            self._recovery_failed = True
            logger.error("トークン再取得後も401が継続したため、復旧失敗として扱います。")

    def report_retry_succeeded(self) -> None:
        """再取得後の再試行が成功した場合に呼び出す。復旧失敗フラグを解除する。"""
        with self._lock:
            self._recovery_failed = False

    @property
    def recovery_failed(self) -> bool:
        """直近の401対応で復旧できなかった状態かどうか。"""
        with self._lock:
            return self._recovery_failed

    def _fetch_locked(self) -> str | None:
        """ロック取得済みの状態でトークンを再取得する（内部用）。"""
        self._last_attempt_at = time.monotonic()
        new_token = self._fetch_token()
        if new_token:
            self._token = new_token
            self._cooldown_until = None
            return new_token
        self._cooldown_until = time.monotonic() + config.KABU_TOKEN_REFRESH_FAILURE_BACKOFF_SECONDS
        self._recovery_failed = True
        logger.error("トークンの再取得に失敗しました。")
        return None


_provider: TokenProvider | None = None
_provider_lock = threading.Lock()


def get_token_provider() -> TokenProvider:
    """プロセス内で共有するTokenProviderのシングルトンを返す。"""
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = TokenProvider()
        return _provider


def reset_token_provider_for_tests(provider: TokenProvider | None = None) -> None:
    """テスト用にシングルトン状態をリセット（または差し替え）する。"""
    global _provider
    with _provider_lock:
        _provider = provider
