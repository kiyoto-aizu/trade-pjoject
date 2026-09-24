# ADR-0004: バックテストの本番相当サイジングをデフォルトにする

- ステータス: Accepted
- 日付: 2026-09-22
- 関連: `docs/adr/0003-backtest-sizing-opt-in.md`, `docs/detail_design/05-backtest-design.md`

## コンテキスト

ADR-0003では、既存の固定数量バックテストとの互換性を優先し、本番相当サイジングを `--production-sizing` によるオプトインとしていた。しかし、週次・累積評価で確認したいのは、本番に近い資金の使い方をした場合の結果である。実行時にフラグを付け忘れると、意図した評価にならない。

## 決定

本番相当サイジングをデフォルトとする。`--target-positions` と `--max-order-amount` が省略された場合は、それぞれ `config.TARGET_POSITIONS` と `config.MAX_ORDER_AMOUNT_PER_TRADE` を使用する。

固定数量でシミュレートしたい場合は `--fixed-qty` を明示し、その場合だけ `--qty` を使用する。`--fixed-qty` と数量サイジングの上書き指定は同時に許可しない。

旧来の `--production-sizing` は互換性のため残すが、非推奨のno-opとし、指定時は警告だけを出す。

## 結果

- 通常のバックテストが本番に近い資金配分を自動的に評価する。
- 固定数量による比較検証も `--fixed-qty` で明示的に継続できる。
- `simulate_backtest` を使う固定銘柄レガシーモードは従来どおり本決定の対象外とする。
