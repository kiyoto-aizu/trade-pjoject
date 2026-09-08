from contextlib import contextmanager
from pathlib import Path

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import fcntl
except ImportError:
    fcntl = None


LOCK_FILE = Path(__file__).resolve().parents[2] / ".market_workflow.lock"


def _lock(handle) -> None:
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    elif fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        raise OSError("この環境ではファイルロックを利用できません。")


def _unlock(handle) -> None:
    if msvcrt is not None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    elif fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def market_workflow_lock():
    """スクリーニング・フィルタリング・取引処理を同時実行しないための排他ロック。"""
    handle = LOCK_FILE.open("a+")
    acquired = False
    try:
        try:
            _lock(handle)
            acquired = True
        except OSError:
            yield False
            return
        yield True
    finally:
        if acquired:
            _unlock(handle)
        handle.close()