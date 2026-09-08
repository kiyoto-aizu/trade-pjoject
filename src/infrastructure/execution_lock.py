from contextlib import contextmanager
from pathlib import Path
import msvcrt


LOCK_FILE = Path(__file__).resolve().parents[2] / ".market_workflow.lock"


@contextmanager
def market_workflow_lock():
    """スクリーニング・フィルタリング・取引処理を同時実行しないための排他ロック。"""
    handle = LOCK_FILE.open("a+")
    acquired = False
    try:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            acquired = True
        except OSError:
            yield False
            return
        yield True
    finally:
        if acquired:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()