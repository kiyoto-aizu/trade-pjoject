# 詳細設計書の不足機能を補う

設計書の対応状況は[docs/README.md](../README.md)にまとめます。コードで確認できない点や実装と設計上の期待との差は、推測で埋めず各設計書の10章に「要確認」として残します。

## 優先度1: 売買に直接効く機能

- [x] **分足バックフィル** — [04 分足バックフィル](../detail_design/04-minute-bar-backfill-design.md)を更新。実行時刻の運用整合・対象日数の数え方・未確定足の扱いは10章に要確認として記録。
- [x] **LLM連携** — [07 LLM連携](../detail_design/07-llm-integration-design.md)を新規作成。日記writerの認証ヘッダーが設定APIキーを使わない実装差などは10章を参照。
- [x] **通知の出し分け・傾向文・中間報告** — [13 通知](../detail_design/13-notification-design.md)を新規作成。Slack配送の再試行・運用設定は10章を参照。
- [x] **フィルタリング上書き** — `run_filtering_override.py`の動作を[02の5章](../detail_design/02-filtering-design.md)に記載。

## 優先度2: 分析系

- [x] **日次分析・トレンドチェック・日足キャッシュ更新** — [08 日次分析](../detail_design/08-daily-analysis-design.md)を新規作成。価格帯別の詳細は03bを参照。
- [x] **週次・月次分析** — [09 週次・月次分析](../detail_design/09-weekly-monthly-analysis-design.md)を新規作成。
- [x] **市場分析・MarketRegime** — [10 市場分析](../detail_design/10-market-analysis-design.md)を新規作成。
- [x] **日次日記** — [11 日次日記](../detail_design/11-daily-diary-design.md)を新規作成。
- [x] **戦略レビュー** — [12 戦略レビュー](../detail_design/12-strategy-review-design.md)を新規作成。

## 優先度3: 診断・保守系

- [x] **板・分足売買代金診断、日次タスク計画・確認** — [14 診断・保守](../detail_design/14-diagnostics-and-maintenance-design.md)を新規作成。既存の`FILTER_TURNOVER_MISSING`調査との関係も記載。

## 設計書化後も残る「要確認」

運用主体・OSタスク登録、通知の再送、日足・分足データの取得境界など、コードから確定できない点は各設計書の「10. 未決・既知の課題」に残しています。設計書の追加作業は完了していますが、これらの運用判断は未完了です。

## 完了条件

- `docs/README.md`の機能→設計書対応表とルートREADMEのリンクが最新である。
- 新規・更新設計書は[共通ひな形](../detail_design/_template.md)の10章構成に揃っている。
- Mermaid構文検証は実施しない。Markdown相対リンク検査は切れ0件を維持する。
