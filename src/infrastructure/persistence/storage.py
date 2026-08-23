"""
================================================================================
永続化ユーティリティモジュール
JSONファイルの読み書きを行う基本的なストレージ機能を提供します。
================================================================================
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def write_json(file_path: Path, data):
    """
    データをJSONファイルに保存します。
    
    Args:
        file_path: 保存先ファイルパス
        data: 保存するデータ
        
    Note:
        エラーが発生した場合はログに記録され、例外は発生しません。
    """
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except OSError:
        logger.exception("Failed to write JSON to %s", file_path)


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