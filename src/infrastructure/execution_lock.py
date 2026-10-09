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


MIDDAY_FILTERING_LOCK_FILE = LOCK_FILE.with_name(".midday_filtering.lock")
MIDDAY_FILTERING_VERIFY_LOCK_FILE = LOCK_FILE.with_name(".midday_filtering_verify.lock")


@contextmanager
def exclusive_file_lock(lock_file: Path):
    """指定ファイルに対する非ブロッキングの排他ロック。取れなければ待たずにFalseを返す。"""
    handle = Path(lock_file).open("a+")
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


@contextmanager
def market_workflow_lock():
    """スクリーニング・フィルタリング・取引処理を同時実行しないための排他ロック。"""
    with exclusive_file_lock(LOCK_FILE) as acquired:
        yield acquired


@contextmanager
def midday_filtering_lock():
    """昼フィルタ専用の排他ロック。取引ループが保持する market_workflow_lock とは干渉しない。"""
    with exclusive_file_lock(MIDDAY_FILTERING_LOCK_FILE) as acquired:
        yield acquired


@contextmanager
def midday_filtering_verify_lock():
    """昼フィルタの検証用実行の専用ロック。本番の昼フィルタ(12:00)とは干渉しない。"""
    with exclusive_file_lock(MIDDAY_FILTERING_VERIFY_LOCK_FILE) as acquired:
        yield acquired
