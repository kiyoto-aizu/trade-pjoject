"""日次のMarketRegime判定を実行します。"""
import json
from dataclasses import asdict
from pathlib import Path

from src.application.market_regime_usecase import MarketRegimeUseCase
from src.config import config
from src.infrastructure.market_data.yahoo_index_client import YahooIndexClient


def main() -> None:
    use_case = MarketRegimeUseCase(
        market_data_client=YahooIndexClient(),
        thresholds=config.MARKET_REGIME_THRESHOLDS,
        realized_volatility_window=config.MARKET_REGIME_REALIZED_VOL_WINDOW,
        data_range=config.MARKET_REGIME_DATA_RANGE,
    )
    assessment = use_case.execute()
    print(json.dumps(asdict(assessment), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()