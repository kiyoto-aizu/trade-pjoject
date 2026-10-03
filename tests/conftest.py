import pytest

from src.config import config
from tests.runtime_guard import RuntimeStateGuard, watch

# モンキーパッチ前の本番パス。番人は常にこちらを監視する。
PRODUCTION_REPORTS_DIRECTORY = config.DAILY_REPORT_DIRECTORY
PRODUCTION_FILTER_DECISION_DATABASE = config.FILTER_DECISION_DATABASE_FILE


@pytest.fixture(autouse=True)
def _isolate_runtime_paths(monkeypatch, tmp_path_factory):
    """既定の出力先(日次レポート・判定イベントDB)をテスト用の一時フォルダへ差し替える。"""
    root = tmp_path_factory.mktemp("isolated_runtime")
    monkeypatch.setattr(config, "DAILY_REPORT_DIRECTORY", root / "reports" / "daily")
    monkeypatch.setattr(config, "FILTER_DECISION_DATABASE_FILE", root / "state" / "filter_decision_events.sqlite3")


@pytest.fixture(autouse=True)
def _production_state_guard(request):
    guard = RuntimeStateGuard(PRODUCTION_REPORTS_DIRECTORY, PRODUCTION_FILTER_DECISION_DATABASE)
    with watch(guard, request.node.nodeid):
        yield


@pytest.fixture(autouse=True, scope="session")
def _production_state_hash_guard():
    guard = RuntimeStateGuard(
        PRODUCTION_REPORTS_DIRECTORY, PRODUCTION_FILTER_DECISION_DATABASE, with_hash=True
    )
    with watch(guard, "テストセッション全体"):
        yield
