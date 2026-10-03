import os

import pytest

from tests.runtime_guard import RuntimeStateGuard, watch

pytest_plugins = ("pytester",)


def _make_guard(tmp_path, with_hash=False):
    reports = tmp_path / "production" / "reports" / "daily"
    reports.mkdir(parents=True)
    (reports / "2026-09-04.json").write_text('{"order_count": 0}', encoding="utf-8")
    database = tmp_path / "production" / "state" / "events.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"db")
    return RuntimeStateGuard(reports, database, with_hash=with_hash), reports, database


def _bump_mtime(path, nanoseconds=5_000_000):
    info = path.stat()
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + nanoseconds))


def test_guard_reports_nothing_when_nothing_changes(tmp_path):
    guard, _, _ = _make_guard(tmp_path)

    assert guard.changes_since(guard.snapshot()) == []


def test_guard_detects_added_removed_and_rewritten_report_files(tmp_path):
    guard, reports, _ = _make_guard(tmp_path)
    before = guard.snapshot()

    (reports / "2026-09-16.json").write_text("{}", encoding="utf-8")
    (reports / "2026-09-04.json").unlink()
    changes = guard.changes_since(before)

    assert any(item.startswith("追加") and "2026-09-16.json" in item for item in changes)
    assert any(item.startswith("削除") and "2026-09-04.json" in item for item in changes)


def test_guard_detects_rewrite_with_identical_content_through_mtime(tmp_path):
    guard, reports, _ = _make_guard(tmp_path)
    before = guard.snapshot()

    target = reports / "2026-09-04.json"
    target.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    _bump_mtime(target)

    assert any("更新" in item for item in guard.changes_since(before))


def test_hash_guard_detects_content_change_even_when_mtime_is_restored(tmp_path):
    stat_guard, reports, database = _make_guard(tmp_path)
    hash_guard = RuntimeStateGuard(reports, database, with_hash=True)
    stat_before = stat_guard.snapshot()
    hash_before = hash_guard.snapshot()

    target = reports / "2026-09-04.json"
    original_ns = target.stat().st_mtime_ns
    target.write_text('{"order_count": 9}', encoding="utf-8")
    os.utime(target, ns=(original_ns, original_ns))

    assert hash_guard.changes_since(hash_before) != []
    assert not any("ハッシュ" in item for item in stat_guard.changes_since(stat_before))


def test_guard_detects_database_creation_and_update(tmp_path):
    guard, _, database = _make_guard(tmp_path)
    before = guard.snapshot()

    _bump_mtime(database)

    assert any("DB" in item for item in guard.changes_since(before))
    database.unlink()
    assert any("DB" in item for item in guard.changes_since(before))


def test_watch_fails_the_test_when_production_state_changes(tmp_path):
    guard, reports, _ = _make_guard(tmp_path)

    with pytest.raises(pytest.fail.Exception, match="本番のデータを書き換えました"):
        with watch(guard, "dummy"):
            (reports / "2026-09-16.json").write_text("{}", encoding="utf-8")


def test_watch_passes_when_production_state_is_untouched(tmp_path):
    guard, _, _ = _make_guard(tmp_path)

    with watch(guard, "dummy"):
        pass


def test_autouse_fixture_makes_a_test_that_writes_to_the_production_path_fail(pytester):
    pytester.makeconftest(
        """
        import pytest
        from pathlib import Path
        from tests.runtime_guard import RuntimeStateGuard, watch

        PRODUCTION = Path(__file__).parent / "fake_production"
        PRODUCTION.mkdir()
        (PRODUCTION / "existing.json").write_text("{}", encoding="utf-8")

        @pytest.fixture(autouse=True)
        def _guard(request):
            guard = RuntimeStateGuard(PRODUCTION, PRODUCTION / "events.sqlite3")
            with watch(guard, request.node.nodeid):
                yield
        """
    )
    pytester.makepyfile(
        """
        from pathlib import Path

        PRODUCTION = Path(__file__).parent / "fake_production"

        def test_writes_to_production():
            (PRODUCTION / "existing.json").write_text('{"changed": true}', encoding="utf-8")

        def test_leaves_production_alone(tmp_path):
            (tmp_path / "report.json").write_text("{}", encoding="utf-8")
        """
    )

    result = pytester.runpytest()

    # 番人の失敗はテスト終了時(teardown)に出るため、pytestはerrorとして数える。
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*本番のデータを書き換えました*"])
