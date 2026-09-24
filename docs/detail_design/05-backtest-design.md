# trade-pjoject 詳細設計書 ⑤バックテストエンジン

対象: 過去データを用いてトレードロジックを疑似的に時間進行させ、損益・勝率・ドローダウン等を評価する機能。
担当ユースケース: `application/backtest_usecase.py`
エントリポイント: `entrypoints/run_backtest.py`（coding-guidelines.md記載の構成に対応、ただし5節参照）

---

## 1. 2つの実行モードと使い分け

`backtest_usecase.py` には評価関数が2つ存在し、想定用途が異なる。

| 関数 | 想定用途 | 対象銘柄の扱い | 指標算出元 |
|---|---|---|---|
| `simulate_backtest` | 固定銘柄リストに対する単純な日足バックテスト（レガシー） | 銘柄リストは全期間固定 | 日足終値のみ |
| `simulate_timeseries_backtest` | 日々のフィルタリング結果を再生する本線バックテスト | `daily_symbols`（日付→銘柄リスト）で**日ごとに対象銘柄が変わる** | `indicator_source`で`daily`/`minute`を選択可 |

- `run_backtest.py`は`--filtering-dir`指定時（＝実運用のフィルタリング結果を使う本来の使い方）は必ず`simulate_timeseries_backtest`を呼ぶ。
- `--filtering-dir`を指定しない固定銘柄モードでも、`--minute-bars-dir`を指定すると`simulate_timeseries_backtest`に切り替わる（分足再生のため日付単位の管理が必要なので）。
- 上記いずれでもない最もシンプルな呼び出し（固定銘柄＋日足のみ）だけが`simulate_backtest`を使う。
- **決定事項**: `simulate_backtest`は動作確認・簡易検証用の位置づけとし、本番相当の評価（③トレードループの再現）は常に`simulate_timeseries_backtest`を正とする。両関数のシグナル判定・約定モデル・出力キーはできる限り重複コピーせず共通化したいが、現状は個別実装されている（8節「既知の課題」参照）。

---

## 2. 処理フロー（`simulate_timeseries_backtest`が主軸）

```
① 事前準備
  → market_regime_by_date が未指定かつ nikkei_market_bars/vix_market_bars が渡されていれば、
    domain/market_regime.py の calculate_market_regime_series[_with_details]() で
    日付ごとのMarketRegime（NORMAL/CAUTION/DANGER）を算出する
  → market_regime_trend_relief_enabled=True（既定）なら、ADXによるトレンド緩和日（trend_relief_dates）も同時に算出
  → market_regime_enabled=False なら market_regime_by_date を強制的に None にし、レジーム連動処理を丸ごと無効化

② 日付ループ（daily_symbols のキー＝営業日を古い順に処理）
  対象銘柄ごとに:
  ②-a 保有中ポジションのチェック（ATR損切り／MarketRegime DANGERでの強制スキップ判定）
  ②-b 指標算出元の分岐
    - indicator_source="daily": その日までの終値からSMA5・RSIを計算
    - indicator_source="minute": minute_bar_repository から分足を取得し、分足ごとにSMA5・RSIを再計算（デイトレ想定でより実勢に近い判定になる）
  ②-c domain/rules.py の calculate_price_limit() / calculate_rsi() でシグナル評価（TradeSignal.evaluate、本番の trading_usecase.py と同一のドメイン関数を使用）
  ②-d BUYシグナル成立時:
    → _adjust_backtest_quantity() で volatility.py の ATR評価（CAUTION/DANGER）に応じて数量を減らす／スキップする（enable_volatility_sizing）
    → MarketRegime=DANGERの日はスキップ（record_filter_decisionでMARKET_REGIME_DANGER_SKIPとして記録）
    → _calculate_execution_price() で約定価格を決定（3節）
    → 資金が足りなければスキップ、足りればcashから減算しholdings/avg_costを更新
  ②-e SELL条件（価格帯上限到達 or 通常%損切り or ATR損切り）成立時: close_position()で決済、実現損益・保有日数・ATR診断を記録
  ②-f close_at_eod=True（デイトレ想定、既定）の場合、日をまたぐ前に強制決済

③ 集計
  → 全期間終了後、保有中ポジションを最終日終値で評価しequityを算出
  → _calculate_metrics() で勝率・利益因子・最大ドローダウンを算出
  → 日別サマリ（daily_summary、日本語キー「日別要約」も同梱）を生成
```

---

## 3. 執行モデル（約定価格のシミュレーション）

シグナル発生と同一バーで即約定させると楽観的すぎるため、以下のパラメータで約定を遅延・劣化させる。

- `execution_delay_bars`（既定1）: シグナル発生バーからNバー後の価格を約定基準にする
- `order_type`: `market`（成行）→ `market_slippage_bps`（既定5bps）を価格に加算（買い）/減算（売り）。`limit`（指値）→ シグナル価格そのものを約定価格として採用（スリッページなし）
- `fee_rate`（既定`config.BACKTEST_FEE_RATE`=0.055%）: 約定代金に対して都度計算し、cashから減算・実現損益からも控除

`_validate_execution_assumptions()`でfee_rate/slippage/delayが負値でないこと、order_typeが`market`/`limit`のいずれかであることを起動時に検証する。

---

## 4. ボラティリティ連動（ATR）とMarketRegime連携

- **数量調整**: `assess_volatility()`（ATR_PERIOD日のATR比率）でNORMAL/CAUTION/DANGERを判定し、`adjust_quantity_for_volatility()`でCAUTION時は`ATR_CAUTION_LOT_RATIO`（既定0.5）に基づき単元単位で減らし、DANGER時は`ATR_DANGER_ACTION`（既定`skip`）に従いスキップまたは最小単位にする。`volatility_stats`に集計を記録し出力に含める。
- **ATR損切り**: `is_atr_stop_loss_triggered()`で、レジーム別の`ATR_STOP_{NORMAL,CAUTION,DANGER}_MULTIPLIER`に応じた損切りラインを判定。固定%損切り（`stop_loss_ratio`）とは独立して評価し、どちらか一方が成立すれば決済する。
- **MarketRegime**: 日経平均VIX・実現ボラティリティから算出したレジームがDANGERの日は新規BUYを一律スキップする（`market_regime_stats`に`danger_skipped`等を記録）。`market_regime_trend_relief_enabled`が有効な場合、ADXが`MARKET_REGIME_ADX_TREND_THRESHOLD`を超えるトレンド局面ではDANGER判定を緩和する日（trend_relief_dates）があり、これも件数を記録する。
- **既知の設計判断**: ATR評価とMarketRegime評価はそれぞれ独立した関数呼び出しになっており、同一日に対して重複してassess_volatility()相当の計算が走る箇所がある（パフォーマンス上の無駄はあるが、正確性には影響しない）。

---

## 5. 本番ロジックとの整合性

バックテストは「本番の判定ロジックをできるだけ再現する」ことが前提。

- **シグナル判定（SMA5/RSI）は本番と共通**: `domain/rules.py`の純粋関数を両方から呼んでいるため、ここは一致している。
- **数量計算は本番相当サイジングをデフォルトにする（ADR-0004、Issue 3対応済み）**: 本番の`trading_usecase.py`は`calculate_buy_quantity`＋残り枠数ベースの予算配分（`wallet_amount / max(TARGET_POSITIONS - open_position_count, 1)`を`MAX_ORDER_AMOUNT_PER_TRADE`でキャップ）で数量を決める。バックテストも既定で、`target_positions`/`max_order_amount_per_trade`を使い、本番と同じ「残り建玉枠で現金按分→上限額でキャップ→単元で丸め」＋「`open_position_count >= target_positions`での新規買いスキップ（`TARGET_POSITIONS_LIMIT_SKIP`として記録）」を適用する。
  - CLI側は`--target-positions`/`--max-order-amount`を個別指定でき、省略時はそれぞれ`config.TARGET_POSITIONS`/`config.MAX_ORDER_AMOUNT_PER_TRADE`を使う。固定数量の比較用途では`--fixed-qty`を指定して`--qty`を使う。旧`--production-sizing`は非推奨no-opとして残す。`simulate_backtest`（レガシー・固定銘柄モード）は対象外。
  - **残課題**: 本番の`wallet_amount`（APIから都度取得する買付可能額）はバックテストでは`cash`変数で代替している。信用取引や買付余力の考え方が変わった場合はこの対応関係を再検討する。`api_soft_limit`（API発注上限）はバックテストには対応する概念がないため未反映（実発注をしないため影響は限定的と判断）。

---

## 6. 指標算出元の切り替え（`indicator_source`）

- `daily`（既定）: その日の終値のみでSMA5・RSIを評価。1日1回の判定に相当し、実際の分足ベースの本番トレードループとは粒度が異なる（`daily_close_signal_evaluations`でカウント）。
- `minute`: `minute_bar_repository`（`ParquetMinuteBarRepository`）から分足を取得し、分足が出るたびにSMA5・RSIを再評価する（`minute_signal_evaluations`でカウント）。本番のトレードループに最も近い粒度だが、分足データが必要なため`run_minute_backfill.py`によるバックフィルが前提となる（④分足バックフィルの設計書を参照）。
- 両モードの評価回数を出力に含めているのは、「どちらの粒度でどれだけ判定が走ったか」を後から検証できるようにするため。

---

## 7. A/Bテスト機能（比較実行）

意思決定を裏付けるため、同一条件で機能ON/OFFを比較する専用関数を用意している。

- `compare_market_regime_backtest()`: MarketRegime有効/無効を同一条件で実行し、total_pnl/total_trades/max_drawdownの差分を返す
- `compare_trend_relief_backtest()`: ADXトレンド緩和の有無を比較
- `run_backtest.py`側にも同等の`--compare-atr`／`--compare-market-regime`フラグがあり、CLIから同様の比較ができる（ただし内部実装は`backtest_usecase.py`の関数を直接呼ばず、entrypoints側で個別に組み立てている＝8節の重複課題）

---

## 8. 出力仕様

`simulate_timeseries_backtest`の戻り値には英語キー（`total_pnl`, `win_rate`等）と日本語キー（`日別要約`）が混在している。`run_backtest.py`側で`display_result`を組み立てる際に日本語ラベルを優先して表示用に変換している。

主なフィールド:
- `cash` / `final_position` / `total_trades` / `total_pnl` / `win_rate` / `max_drawdown` / `profit_factor`
- `signals`（発注イベントの配列。決済有無に関わらず1発注＝1件）
- `trade_history`（決済が成立した取引のみ。ATR診断情報`atr_diagnostic`を含む）
- `daily_summary`（日別の決済損益集計）
- `execution_assumptions`（fee_rate/order_type/slippage/delay_barsのスナップショット。後から「どの前提で出た結果か」を追跡するため）
- `volatility_adjustment` / `market_regime_adjustment`（各機能の適用統計）

---

## 9. 既知の課題（coding-guidelines.md逸脱・技術的負債）

- **entrypointsが「薄いラッパー」の責務を超えている**: `run_backtest.py`（628行）はYahoo Financeからの取得関数（`fetch_yahoo_history`/`fetch_yahoo_dated_history`/`fetch_yahoo_dated_ohlc`）や、比較結果の整形関数（`_comparison_summary`/`_market_regime_comparison_summary`）を直接持っている。coding-guidelines.md 5節は「市場セッション判定・ロック・事前データ取得までを行う場合は薄いラッパーの責務を超えるため、application側へ移すか、例外として設計書に明記する」としているため、本設計書をもってこれを正式な例外として明記する。ただし将来的には`infrastructure/market_data/`配下（既存の`YahooFinanceClient`等）へ寄せて重複を解消したい。
- **`simulate_backtest`と`simulate_timeseries_backtest`のロジック重複**: 約定価格計算・ATR評価・シグナル判定など多くの処理が両関数にほぼ同じ形で存在する。1節の通り本線は`simulate_timeseries_backtest`なので、`simulate_backtest`を薄いラッパー（内部で`daily_symbols`を全期間固定で組み立てて`simulate_timeseries_backtest`を呼ぶ形）に置き換えられないか、次回リファクタリング時に検討する。
- **`run_backtest.py`内の比較ロジック（`_comparison_summary`等）が`backtest_usecase.py`の`compare_*_backtest()`と役割が重複**している。CLIオプション（`--compare-atr`等）とライブラリ関数側で別々にA/Bテストを組み立てており、将来どちらかに統一したい。

---

## 10. 決定事項サマリ

- 本番相当の評価は常に`simulate_timeseries_backtest`（日次フィルタリング結果の再生）を正とする
- 約定モデルは「Nバー遅延＋成行スリッページ／指値はスリッページなし」で固定
- ATR/MarketRegimeの適用有無はいずれもデフォルトで有効（`enable_volatility_adjustment=True`, `market_regime_enabled=True`）
- 数量計算はデフォルトで本番相当の予算配分を使い、固定数量が必要な場合だけ`--fixed-qty`を指定する（ADR-0004）
- 週次など期間を完全一致させる評価では、`--start-date`と`--end-date`を両方指定する。未指定時は`--days`による相対カットオフを使う（ADR-0005）
- entrypointsの肥大化（データ取得・比較整形の内包）は、coding-guidelines.md 5節の「例外」として本設計書で正式に許容する
