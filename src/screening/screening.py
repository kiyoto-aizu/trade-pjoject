"""静的スクリーニングの雛形。

実際の取得ロジック（API/スクレイピング/CSV）は実装箇所に置換してください。
"""
import logging
from pathlib import Path

from src.application.screening_usecase import ScreeningUseCase

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / 'data'
DATA_DIR.mkdir(exist_ok=True)

def run(usecase: ScreeningUseCase):
    result = usecase.execute()
    logger.info("screening finished: %d symbols", len(result.symbols))
    return result
