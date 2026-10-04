"""
================================================================================
永続化ユーティリティモジュール
JSONファイルの読み書きを行う基本的なストレージ機能を提供します。
================================================================================
"""
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class StateFileCorruptError(Exception):
    """既存の状態ファイルが読めない・不正な場合の例外。"""

    def __init__(self, path: Path, cause: str, copy_path: Path | None = None):
        self.path = Path(path)
        self.cause = cause
        self.copy_path = copy_path
        copy_note = f" 破損ファイルのコピー: {copy_path}。" if copy_path else ""
        super().__init__(
            f"状態ファイルが破損しているか読み込めません: {self.path} (原因: {cause})。{copy_note}"
            "原因を確認し、ファイルを修正するか、意図的に初期化する場合はファイルを削除してください。"
        )


def write_json(file_path: Path, data) -> bool:
    """
    データをJSONファイルに保存します(一時ファイル経由の原子的な置き換え)。

    Args:
        file_path: 保存先ファイルパス
        data: 保存するデータ

    Returns:
        保存に成功した場合True、失敗した場合False

    Note:
        エラーが発生した場合はログに記録され、例外は発生しません。
    """
    tmp_path = Path(f"{file_path}.tmp")
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, file_path)
        return True
    except OSError:
        logger.exception("Failed to write JSON to %s", file_path)
        _remove_quietly(tmp_path)
        return False
    except BaseException:
        # シリアライズ失敗等のOSError以外は従来どおり呼び出し元へ伝える
        _remove_quietly(tmp_path)
        raise


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.exception("Failed to remove temporary file %s", path)


def preserve_corrupt_copy(file_path: Path) -> Path | None:
    """破損ファイルを同じディレクトリへコピーして残す。失敗時はログのみでNoneを返す。"""
    file_path = Path(file_path)
    copy_path = file_path.with_name(f"{file_path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}")
    try:
        shutil.copy2(file_path, copy_path)
        return copy_path
    except OSError:
        logger.exception("Failed to copy corrupt file %s", file_path)
        return None


def read_json_strict(file_path: Path):
    """
    JSONファイルを厳密に読み込みます。

    ファイルが存在しない場合のみNoneを返します(初回起動)。
    読み込み失敗・JSON破損・最上位がdictでない場合は、破損ファイルのコピーを残して
    StateFileCorruptErrorを送出します。
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.exception("State file is unreadable: %s", file_path)
        raise StateFileCorruptError(
            file_path, f"{type(exc).__name__}: {exc}", preserve_corrupt_copy(file_path)
        ) from exc
    if not isinstance(data, dict):
        raise StateFileCorruptError(
            file_path,
            f"最上位がオブジェクトではありません({type(data).__name__})",
            preserve_corrupt_copy(file_path),
        )
    return data


def read_json(file_path: Path):
    """
    JSONファイルからデータを読み込みます。
    
    Args:
        file_path: 読み込むファイルパス
        
    Returns:
        読み込んだデータ、ファイルが存在しない場合やエラー時はNone
        
    Note:
        エラーが発生した場合はログに記録され、Noneが返されます。
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        logger.exception("JSON decode error reading %s", file_path)
        return None
    except OSError:
        logger.exception("Failed to read JSON from %s", file_path)
        return None