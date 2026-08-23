"""
================================================================================
スクリーニング実行エントリーポイント
銘柄スクリーニング処理を実行するスクリプト層です。

注意：
実際の取得ロジック（API/スクレイピング/CSV読み込み）は
実装箇所に置換してください。
================================================================================
"""
import logging
from pathlib import Path

from src.application.screening_usecase import ScreeningUseCase

logger = logging.getLogger(__name__)

# データディレクトリの作成（存在しない場合）
DATA_DIR = Path(__file__).resolve().parents[2] / 'data'
DATA_DIR.mkdir(exist_ok=True)


def run(usecase: ScreeningUseCase):
    """
    スクリーニング処理を実行します。
    
    Args:
        usecase: ScreeningUseCaseインスタンス
        
    Returns:
        ScreeningResultオブジェクト
    """
    result = usecase.execute()
    logger.info("screening finished: %d symbols", len(result.symbols))
    return result
