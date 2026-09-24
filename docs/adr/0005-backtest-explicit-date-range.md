# ADR-0005: 週次バックテストの評価期間を絶対日付で指定する

- ステータス: Accepted
- 日付: 2026-09-22
- 関連: `src/entrypoints/run_weekly_analysis.py`, `src/infrastructure/analysis/summary_loader.py`, `docs/detail_design/05-backtest-design.md`

## コンテキスト

週次分析の `exact_period_run_available` は、バックテスト結果の `period_start` と `period_end` が分析対象週の月曜・金曜に完全一致することを前提とする。相対日数の `--days` だけでは、祝日や実行タイミングによってこの一致を保証できない。

## 決定

`run_backtest.py` に `--start-date` と `--end-date`（いずれも `YYYY-MM-DD`）を追加する。使用時は両方を必須とし、`--filtering-dir` の日次シンボルを指定範囲の両端を含めてフィルタする。両方が指定された場合は相対的な `--days` より優先する。指定しない場合は従来どおり `--days` による相対カットオフを使う。

`--start-date` と `--end-date` の指定範囲が、そのまま `simulate_timeseries_backtest` の `period_start` と `period_end` になることをテストで保証する。

## 既知の制約

祝日により対象週の取引日が5日に満たない場合、日次フィルタリング結果自体のキーが月曜または金曜を含まず、結果の期間境界が指定日と一致しない可能性がある。祝日を推定して日付を補完することは、このバックテストの入力データを捏造するため行わない。
