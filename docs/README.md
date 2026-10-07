# ドキュメント案内

このページを`docs/`の入口として、資料の置き場所と機能ごとの設計書を案内します。

## フォルダの役割

| フォルダ | 内容 |
|---|---|
| [`architecture/`](./architecture/) | 全体構成・データフロー・実行スケジュール・通知・コーディング規約 |
| [`reference/`](./reference/) | 設定項目などの生成参照資料 |
| [`detail_design/`](./detail_design/) | 機能ごとの詳細設計書と[_template.md](./detail_design/_template.md) |
| [`adr/`](./adr/) | 設計上の決定記録 |
| [`tasks/`](./tasks/) | 未完了・完了の改修タスク |
| [`reviews/`](./reviews/) | コード・戦略のレビュー記録と検証結果 |
| [`prompts/`](./prompts/) | 現在は未配置。新しい実装依頼プロンプトの置き場 |
| [`archive/`](./archive/) | 現行仕様ではない過去資料・置き換え済みプロンプト |

### architecture資料

- [全体データフロー](./architecture/flow.md)
- [実行スケジュール](./architecture/schedule.md)
- [Slack通知](./architecture/notifications.md)
- [コーディング規約](./architecture/coding-guidelines.md)

## 機能と詳細設計書

`src/entrypoints/*.py`を基準に、起動スクリプトから設計書・図をたどれるようにしています。「設計書なし」は詳細設計書が未整備、「一部未反映」は記載範囲が機能全体を覆っていない状態です。

| 機能 | 起動スクリプト | 設計書 | 図 | 状態 |
|---|---|---|---|---|
| バックテストv2複数日検証 | `backtest_v2_multi_day_check.py` | [06 ヒストリカル再生](./detail_design/06-trading-usecase-historical-replay.md) | 4章 | 一部未反映 |
| バックテストv2単日検証 | `backtest_v2_single_day_check.py` | [06 ヒストリカル再生](./detail_design/06-trading-usecase-historical-replay.md) | 4章 | 一部未反映 |
| 板売買代金診断 | `diagnose_board_turnover.py` | なし | なし | 設計書なし |
| 分足売買代金欠損診断 | `diagnose_missing_turnover_by_minute_bars.py` | なし | なし | 設計書なし |
| ADX分析 | `run_adx_analysis.py` | なし | なし | 設計書なし |
| ATR比率分析 | `run_atr_ratio_analysis.py` | なし | なし | 設計書なし |
| バックテスト | `run_backtest.py` | [05 バックテスト](./detail_design/05-backtest-design.md) | 4章 | 現行 |
| 日次分析 | `run_daily_analysis.py` | [03b 価格帯別トレンド答え合わせ](./detail_design/03b-price-band-trend-check.md)（価格帯別の一部） | 4章 | 一部未反映 |
| 日次日記 | `run_daily_diary.py` | なし | なし | 設計書なし |
| 日次タスク確認 | `run_daily_task_check.py` | なし | なし | 設計書なし |
| 日次タスク計画 | `run_daily_task_plan.py` | なし | なし | 設計書なし |
| フィルタリング | `run_filtering.py` | [02 フィルタリング](./detail_design/02-filtering-design.md) | 4章 | 現行 |
| フィルタリング上書き | `run_filtering_override.py` | なし | なし | 設計書なし |
| 市場レジーム分析 | `run_market_regime.py` | なし | なし | 設計書なし |
| 市場ボラティリティ分析 | `run_market_volatility_analysis.py` | なし | なし | 設計書なし |
| 分足バックフィル | `run_minute_backfill.py` | [04 分足バックフィル](./detail_design/04-minute-bar-backfill-design.md) | 4章 | 一部未反映 |
| 月次分析 | `run_monthly_analysis.py` | なし | なし | 設計書なし |
| スクリーニング | `run_screening.py` | [01 スクリーニング](./detail_design/01-screening-design.md) | 4章 | 現行 |
| 戦略レビュー | `run_strategy_review.py` | なし | なし | 設計書なし |
| 取引ループ | `run_trading.py` | [03 取引ループ](./detail_design/03-trading-loop-design.md) | 4章 | 現行 |
| トレンドチェック | `run_trend_check.py` | なし | なし | 設計書なし |
| VIX分析 | `run_vix_analysis.py` | なし | なし | 設計書なし |
| 週次分析 | `run_weekly_analysis.py` | なし | なし | 設計書なし |
| 日足キャッシュ更新 | `update_daily_bar_cache.py` | なし | なし | 設計書なし |
| Slack通知の出し分け・開始通知の傾向文 | 複数entrypoints | なし（architecture資料のみ） | なし | 詳細設計書なし |
| LLM連携（07相当・欠番） | 日次・週次・月次分析、戦略レビュー等から利用 | なし | なし | 設計書なし |

## 運用ルール

- 新機能を追加したら、この対応表とルートの[README.md](../README.md)のドキュメントリンクも更新してください。
- 詳細設計書は[共通ひな形](./detail_design/_template.md)から作成し、章番号と章名を揃えてください。
- 全体構成・データフロー・実行スケジュール・Slack通知は[`architecture/`](./architecture/)の各資料を更新してください。
