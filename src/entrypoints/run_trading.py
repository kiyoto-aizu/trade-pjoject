"""
================================================================================
取引実行エントリーポイント
フィルタ、リング結果を基に自動取引を実行します。
================================================================================
"""
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from pathlib import Path

from src.config import config
from src.application.trading_usecase import TradingUseCase
from src.domain.rules import is_market_closed, is_trading_session
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.notification.line_notify import process_notification
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.execution_lock import market_workflow_lock
from src.infrastructure.notification.line_notify import send_line_notify
from src.infrastructure.paper.paper_order_executor import PaperOrderExecutor


def create_trading_use_case(token: str) -> TradingUseCase:
    """実行モードに応じた取引ユースケースを組み立てます。"""
    root = Path(__file__).resolve().parents[2]
    order_sender = None
    if config.TRADING_MODE == 'paper':
        order_sender = PaperOrderExecutor(
            prices={},
            cash=config.OPERATING_CAPITAL,
            state_path=root / config.PAPER_ACCOUNT_STATE_FILE,
        )
    elif config.TRADING_MODE != 'live' or config.IS_DEMO or not config.ENABLE_LIVE_ORDERING:
        raise ValueError(
            'ライブ注文にはTRADING_MODE=live、IS_DEMO=false、ENABLE_LIVE_ORDERING=trueが必要です。'
        )
    return TradingUseCase(
        token=token,
        order_history_path=root / config.ORDER_HISTORY_FILE,
        order_sender=order_sender,
        filtering_result_repository=FilteringResultRepository(root / 'data' / 'filtering'),
        notifier=send_line_notify,
    )


def configure_logging() -> None:
    """ロギングを設定します。"""
    logging.root.handlers.clear()
    log_level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    logging.root.setLevel(log_level)

    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logging.root.addHandler(stream_handler)

    file_handler = TimedRotatingFileHandler(
        config.LOG_FILE_PATH,
        when='midnight',
        interval=1,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding='utf-8',
    )
    file_handler.setFormatter(formatter)
    logging.root.addHandler(file_handler)


def main(now_provider=None) -> None:
    """
    取引ボットを起動します。
    
    処理フロー：
    1. ロギングを設定
    2. APIトークンを取得
    3. TradingUseCaseを組み立てて実行
    """
    configure_logging()
    logging.getLogger(__name__).info('取引モード: %s', config.TRADING_MODE_LABEL)
    with market_workflow_lock() as acquired:
        if not acquired:
            logging.getLogger(__name__).warning("他の市場処理が実行中のため、トレーディングを中止します。")
            return
        with process_notification(
            '取引',
            trigger='フィルタリング結果',
            start_detail=f'開始（{config.TRADING_MODE_LABEL}）',
        ):
            now = (now_provider or datetime.now)()
            if not is_trading_session(
                now,
                config.MARKET_OPEN_HOUR,
                config.MARKET_OPEN_MINUTE,
                config.MARKET_CLOSE_HOUR,
                config.MARKET_CLOSE_MINUTE,
            ):
                logging.getLogger(__name__).info('市場時間外または休場日のため、取引を開始しません。')
                return

            filtering_repository = FilteringResultRepository(Path(__file__).resolve().parents[2] / 'data' / 'filtering')
            filtering_result = filtering_repository.load_for_date(now.date())
            if not filtering_result or not filtering_result.symbols:
                logging.getLogger(__name__).info('当日のフィルタ結果がないため、取引を開始しません。')
                return

            token = get_api_token()
            if not token:
                raise SystemExit('トークン取得に失敗しました。')

            use_case = create_trading_use_case(token)
            if not config.ALLOW_OVERNIGHT_HOLDING and is_market_closed(
                now.time(),
                config.MARKET_LIQUIDATION_HOUR,
                config.MARKET_LIQUIDATION_MINUTE,
            ):
                use_case.run()
            else:
                market_data = use_case.collect_preflight_market_data(filtering_result.symbols)
                if market_data is None:
                    logging.getLogger(__name__).error('市場データまたは板情報を取得できないため、取引を開始しません。')
                    return
                use_case.run(preflight_market_data=market_data)


if __name__ == '__main__':
    main()
