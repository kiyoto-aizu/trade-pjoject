"""
================================================================================
フィルタリング実行エントリーポイント
出来高急騰銘柄抽出処理を実行するスクリプト層です。
================================================================================
"""
import logging
from src.application.filtering_usecase import FilteringUseCase

logger = logging.getLogger(__name__)


def run(usecase: FilteringUseCase):
    """
    フィルタリング処理を実行します。
    
    Args:
        usecase: FilteringUseCaseインスタンス
        
    Returns:
        FilteringResultオブジェクト
    """
    result = usecase.execute()
    logger.info("filtering finished: %d symbols", len(result.symbols))
    return result
