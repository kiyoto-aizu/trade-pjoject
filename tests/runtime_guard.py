"""テストが本番の日次レポートや判定イベントDBを書き換えていないかを検出する番人。"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest


@dataclass(frozen=True)
class Snapshot:
    report_stats: dict[str, tuple[int, int]]
    report_hashes: dict[str, str] = field(default_factory=dict)
    database_mtime_ns: int | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RuntimeStateGuard:
    """data/reports配下の全ファイルと判定イベントDBの更新日時を記録して比べる。"""

    def __init__(self, reports_directory: Path, database_file: Path, with_hash: bool = False):
        self.reports_directory = Path(reports_directory)
        self.database_file = Path(database_file)
        self.with_hash = with_hash

    def snapshot(self) -> Snapshot:
        stats: dict[str, tuple[int, int]] = {}
        hashes: dict[str, str] = {}
        if self.reports_directory.exists():
            for path in sorted(self.reports_directory.rglob("*")):
                if not path.is_file():
                    continue
                key = path.relative_to(self.reports_directory).as_posix()
                info = path.stat()
                stats[key] = (info.st_size, info.st_mtime_ns)
                if self.with_hash:
                    hashes[key] = _sha256(path)
        database_mtime = self.database_file.stat().st_mtime_ns if self.database_file.exists() else None
        return Snapshot(stats, hashes, database_mtime)

    def changes_since(self, before: Snapshot) -> list[str]:
        after = self.snapshot()
        changes = []
        for key in sorted(set(before.report_stats) | set(after.report_stats)):
            if key not in before.report_stats:
                changes.append(f"追加: {self.reports_directory / key}")
            elif key not in after.report_stats:
                changes.append(f"削除: {self.reports_directory / key}")
            elif before.report_stats[key] != after.report_stats[key]:
                changes.append(f"更新(サイズまたは更新日時): {self.reports_directory / key}")
            elif self.with_hash and before.report_hashes.get(key) != after.report_hashes.get(key):
                changes.append(f"内容変更(ハッシュ不一致): {self.reports_directory / key}")
        if before.database_mtime_ns != after.database_mtime_ns:
            changes.append(f"DBの更新日時が変化: {self.database_file}")
        return changes


@contextmanager
def watch(guard: RuntimeStateGuard, label: str):
    """ブロック内で本番の状態が変わったら、終了時にテストを失敗させる。"""
    before = guard.snapshot()
    yield
    changes = guard.changes_since(before)
    if changes:
        pytest.fail(
            f"{label}: テストが本番のデータを書き換えました。tmp_pathへ隔離してください。\n" + "\n".join(changes),
            pytrace=False,
        )
