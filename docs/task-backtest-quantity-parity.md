# 改修タスク: バックテストの数量計算を本番の予算配分ロジックに合わせる（Issue 3）

- 起票日: 2026-09-18
- 対象: `src/application/backtest_usecase.py`（`simulate_timeseries_backtest`）, `src/entrypoints/run_backtest.py`, `tests/test_backtest.py`
- 関連設計書: `docs/detail_design/05-backtest-design.md` 5節「本番ロジックとの整合性（既知の乖離）」
- ステータス: **対応済み（要レビュー・要マージ）**

## 問題

本番（`trading_usecase.py`）は買い数量を以下のロジックで決定する。

```python
remaining_slots = max(config.TARGET_POSITIONS - open_position_count, 1)
budget_per_position = min(
    wallet_amount / remaining_slots,
    config.MAX_ORDER_AMOUNT_PER_TRADE,
    api_soft_limit,
)
qty = calculate_buy_quantity(price, budget_per_position, config.ORDER_UNIT)
```

さらに `open_position_count >= config.TARGET_POSITIONS` の場合は新規買いを見送る同時保有数の上限チェックがある。

一方バックテスト（`simulate_timeseries_backtest`）はCLIの`--qty`で与えた**固定数量**にATRボラティリティ調整のみを適用しており、上記の「残り建玉枠での予算按分」「1回あたり上限額」「同時保有数の上限」のいずれも反映していなかった。そのためバックテストの損益・勝率・ドローダウンは、資金効率まで含めた本番相当の期待値を表していない。

## 対応内容

`simulate_timeseries_backtest` に以下のオプション引数を追加し、**未指定時は従来の固定数量モードのまま**（後方互換）とした上で、指定時のみ本番相当の予算配分ロジックに切り替える方式にした。

- `target_positions: int | None = None` — 同時保有銘柄数の上限。指定時のみ動的配分モードが有効になる
- `max_order_amount_per_trade: float | None = None` — 1回あたりの発注上限額
- `order_unit: int = config.ORDER_UNIT` — 売買単位

`target_positions`指定時の挙動:

1. 新規買いのたびに `open_position_count = 保有銘柄数` を数え、`target_positions`以上なら**新規買いをスキップ**（`TARGET_POSITIONS_LIMIT_SKIP`としてfilter_decisionに記録）
2. `remaining_slots = max(target_positions - open_position_count, 1)` で残り枠を計算
3. `budget_per_position = cash / remaining_slots`（`max_order_amount_per_trade`指定時はさらにそれでキャップ）
4. `calculate_buy_quantity(price, budget_per_position, order_unit)` で数量を算出し、既存のATRボラティリティ調整（`_adjust_backtest_quantity`）を従来通り適用

`run_backtest.py`には以下のCLIオプションを追加した。

- `--production-sizing`: 上記モードを有効化し、`target_positions`/`max_order_amount_per_trade`の既定値としてそれぞれ`config.TARGET_POSITIONS`/`config.MAX_ORDER_AMOUNT_PER_TRADE`を使う
- `--target-positions` / `--max-order-amount`: 個別に上書き指定したい場合用

`simulate_timeseries_backtest`を呼ぶ8箇所（本線・MarketRegime比較・ATR比較の各baseline/lot_only含む）すべてに`**sizing_kwargs`を反映済み。レガシーの`simulate_backtest`（固定銘柄モード）は対象外とし、`--filtering-dir`未指定時にこれらのオプションを指定した場合は警告ログを出す。

## 変更ファイル

- `src/application/backtest_usecase.py`
- `src/entrypoints/run_backtest.py`
- `tests/test_backtest.py`（新規3件、既存29件はすべて非破壊で通過。全体232件パス確認済み）

## 使い方（今週末のバックテスト実行時）

```bash
python -m src.entrypoints.run_backtest \
  --live --filtering-dir data/filtering \
  --production-sizing \
  --output data/backtest/result_production_sizing.json
```

`--target-positions`/`--max-order-amount`を省略すると、現在の本番設定値（`config.TARGET_POSITIONS`, `config.MAX_ORDER_AMOUNT_PER_TRADE`）がそのまま使われる。

## 未解消・要検討事項（次回以降）

- `wallet_amount`は本番ではAPIから都度取得する「現物買付可能額」だが、バックテストでは`cash`（バックテスト内の残高変数）で代替している。信用取引や制度上の買付余力の違いが将来出てきた場合は、この対応関係を再検討する必要がある
- `api_soft_limit`（API発注上限によるキャップ）は本番の`budget_per_position`計算に含まれるが、バックテストには対応する概念がないため反映していない（バックテストではそもそもAPI発注をしないため、影響は限定的と判断）
- `simulate_backtest`（レガシー・固定銘柄モード）には今回のロジックを入れていない。05番設計書の決定事項通り「本番相当の評価は常に`simulate_timeseries_backtest`」という前提を維持する
