"""
================================================================================
フィルタリング手動実行エントリーポイント(スクリーニング参照日オーバーライド版)
祝日等でスクリーニングが欠落し、通常の「前営業日」算出では
正しいスクリーニング結果を参照できない場合に、参照する
スクリーニング結果の日付を明示的に指定してフィルタリングを
手動実行するための運用ツールです。板情報・出来高は本日のライブ
データを使用します(バックテストではありません)。
================================================================================
"""
import argparse
import logging
from datetime import date
from pathlib import Path

from src.application.filtering_usecase import FilteringUseCase
from src.entrypoints.run_filtering import BoardClient, configure_logging, notify_result
from src.infrastructure.execution_lock import market_workflow_lock
from src.infrastructure.kabu.get_token import get_api_token
from src.infrastructure.kabu.unregister import unregister_all
from src.infrastructure.market_data.yahoo_finance_client import YahooFinanceClient
from src.infrastructure.notification.slack_notify import process_notification
from src.infrastructure.persistence.filtering_result_repository import FilteringResultRepository
from src.infrastructure.persistence.screening_result_repository import ScreeningResultRepository


class FixedDateScreeningRepository:
    """常に指定した1つの日付のスクリーニング結果を返すラッパー。"""

    def __init__(self, inner_repository: ScreeningResultRepository, screening_date: date):
        self.inner_repository = inner_repository
        self.screening_date = screening_date

    def load_for_date(self, _target_date: date):
        return self.inner_repository.load_for_date(self.screening_date)


def main() -> None:
    """
    指定日のスクリーニング結果を参照して、本日のライブ板情報で
    フィルタリングを手動実行します。
    """
    parser = argparse.ArgumentParser(
        description="指定日のスクリーニング結果を参照して、本日のライブ板情報でフィルタリングを実行します。"
    )
    parser.add_argument(
        "--screening-date",
        dest="screening_date",
        type=date.fromisoformat,
        required=True,
        help="参照するスクリーニング結果の日付 (YYYY-MM-DD)",
    )
    args = parser.parse_args()

    configure_logging()
    logging.getLogger(__name__).info(
        "手動実行: %sのスクリーニング結果を参照してフィルタリングを実行します。", args.screening_date
    )
    with market_workflow_lock() as acquired:
        if not acquired:
            logging.getLogger(__name__).warning("他の市場処理が実行中のため、フィルタリングを中止します。")
            return
        with process_notification(
            'フィルタリング(手動/参照日指定)',
            notify_lifecycle=False,
            trigger=f'手動実行(参照スクリーニング日: {args.screening_date.isoformat()})',
        ):
            root = Path(__file__).resolve().parents[2] / 'data'
            token = get_api_token()
            if not token:
                raise SystemExit('トークン取得に失敗しました。')
            if unregister_all(token) is None:
                raise SystemExit('銘柄登録の全解除に失敗しました。')

            screening_repository = FixedDateScreeningRepository(
                ScreeningResultRepository(root / 'screening'),
                args.screening_date,
            )
            usecase = FilteringUseCase(
                screening_repository,
                BoardClient(token),
                YahooFinanceClient(),
                FilteringResultRepository(root / 'filtering'),
                notify_result,
            )
            # target_date=Noneでライブ板情報の分岐(本日実行)を使う。
            # スクリーニング結果は上のラッパーにより常にargs.screening_dateのものが使われる。
            usecase.execute(target_date=None)


if __name__ == '__main__':
    main()
