# 詳細設計書の不足機能を補う

設計書の対応状況は[docs/README.md](../README.md)にまとめる。実装内容の確認が必要な項目は、コードを確認してから記載し、不明点は「要確認」とする。

## 優先度1: 売買に直接効く機能

- [ ] **分足バックフィル** — [04 分足バックフィル](../detail_design/04-minute-bar-backfill-design.md)は一部未反映。現行のParquet保存、取得・補強の条件、スケジュールとの整合など、設計書内の「要確認」をコードと運用資料で確認する。
- [ ] **LLM連携（07相当・欠番）** — 分析・戦略レビュー等の呼び出し元、入力、プロンプト、出力、失敗時の扱いを洗い出し、独立した詳細設計書を作成する。現時点で設計書07はない。
- [ ] **Slack通知の出し分け・開始通知の傾向文** — [architecture/notifications.md](../architecture/notifications.md)の一覧を基点に、売買・分析の通知条件、通知先、時刻、内容を詳細化する。コードで確定できない挙動は「要確認」とする。
- [x] **フィルタリング上書き** — `run_filtering_override.py`の入力・指定日ラッパー・当日ライブデータ・結果出力・通常フィルタとの関係を[02の5章](../detail_design/02-filtering-design.md)に記載した。

## 優先度2: 分析系

- [ ] **日次分析全体** — `run_daily_analysis.py`の設計を補う。03bは価格帯別トレンド答え合わせ部分のみを扱う。
- [ ] **トレンドチェック** — `run_trend_check.py`
- [ ] **ADX分析** — `run_adx_analysis.py`
- [ ] **ATR比率分析** — `run_atr_ratio_analysis.py`
- [ ] **日次日記** — `run_daily_diary.py`
- [ ] **市場レジーム分析** — `run_market_regime.py`
- [ ] **市場ボラティリティ分析** — `run_market_volatility_analysis.py`
- [ ] **月次分析** — `run_monthly_analysis.py`
- [ ] **戦略レビュー** — `run_strategy_review.py`
- [ ] **VIX分析** — `run_vix_analysis.py`
- [ ] **週次分析** — `run_weekly_analysis.py`

## 優先度3: 診断・保守系

- [ ] **板売買代金診断** — `diagnose_board_turnover.py`
- [ ] **分足売買代金欠損診断** — `diagnose_missing_turnover_by_minute_bars.py`
- [ ] **日足キャッシュ更新** — `update_daily_bar_cache.py`
- [ ] **日次タスク計画** — `run_daily_task_plan.py`
- [ ] **日次タスク確認** — `run_daily_task_check.py`

## 完了条件

- `docs/README.md`の機能→設計書対応表が最新で、設計書なし・一部未反映の項目が実態と一致している。
- 新規・更新設計書は[共通ひな形](../detail_design/_template.md)の10章構成に揃っている。
- 各段階でMarkdown相対リンクを検査し、既存のリンク切れ件数を増やさない。
