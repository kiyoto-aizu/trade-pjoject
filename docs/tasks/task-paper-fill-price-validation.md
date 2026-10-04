# 改修タスク: ペーパー約定価格の入力検証

- 起票日: 2026-10-04
- 対象: `src/infrastructure/paper/paper_order_client.py`、`src/application/trading_usecase.py`、`src/infrastructure/kabu/board_repository.py`、ヒストリカル再生クライアント、関連テスト
- ステータス: **完了（2026-10-04）**
- 実施時期: **2026-10-04**
- 関連調査: 2026-10-02「売買部分サイレントスキップ横並び調査」。第1弾は`ORDER_REJECTED_NONE` / `ORDER_REJECTED_RESULT`等を`DecisionJournalRepository.TRADING_REASON_CODES`に記録する。価格値の不正を区別する理由コードはまだない。

## 背景

`PaperOrderClient`は入力価格を検証せず、手数料・スリッページ込みの約定計算に利用する。価格の欠落と、数値として不正な価格の扱いが異なり、経路によっては古い`prices[symbol]`が残ったまま使われる。

緊急停止経路はfreshness検証なしで清算する。現行の`_liquidate_all_positions()`非fresh分岐（`src/application/trading_usecase.py:1225-1249`）では、板価格が0以下のとき`EOD_PRICE_UNAVAILABLE`をWARNING記録するだけで`set_price()`を呼ばず、そのまま注文メソッドへ進む。PaperOrderClientに前回の価格が残っていれば、その古い価格で模擬売りが成立し得る。

## 現行コードで確認した挙動

| 経路 | 実装・位置 | 現行挙動 |
|---|---|---|
| 通常の買い・売り | `TradingUseCase.run()`（`src/application/trading_usecase.py:1961`）から`set_price()`、`PaperOrderClient.place_market_order()`（同`src/infrastructure/paper/paper_order_client.py:67,70`） | 現在値を設定して注文する。`place_market_order()`は価格が`None`なら`None`を返すが、0以下・NaN・infを明示的には拒否しない。成立時は価格からスリッページ・手数料を計算して残高・保有を更新する。 |
| 通常板情報 | `BoardRepository.get_current_board()`（`src/infrastructure/kabu/board_repository.py:29`） | `CurrentPrice`等を返すが、通常経路の戻り値に`CurrentPriceTime`・`CurrentPriceStatus`は含めない。 |
| 15:20決済・15:30以降の遅延決済 | `_liquidate_all_positions(require_fresh_price=True)`（`src/application/trading_usecase.py:1446,1454`）と`_fresh_liquidation_quote()`（同1110） | `CurrentPriceTime`の当日性、status 1/8、価格の有限・正値を検証し、失敗時は決済注文を出さず未決済記録・通知を行う。これは既存の[本番EOD決済・約定価格タスク](./task-live-eod-liquidation-and-fill-reconciliation.md)の対象と重なるため、本タスクでは変更範囲に含めない。 |
| 緊急停止時の清算 | `run()`（`src/application/trading_usecase.py:1435`）から`_liquidate_all_positions()`をfreshness要求なしで呼ぶ | 板価格が0以下ならWARNINGのみ記録し、`set_price()`を呼ばず注文へ進む。PaperOrderClientに以前設定された価格が残っていれば、その価格で模擬売りが成立し得る。 |
| ヒストリカル再生 | `HistoricalBoardClient.get_current_board()` / `get_current_board_with_freshness()`（`src/infrastructure/backtest/historical_clients.py:86,97`） | シミュレーション時計時点までの分足価格を返す。freshness版は分足時刻・status 1も返す。実時間の「古さ」をそのまま適用すると、履歴再生の時刻意味を壊す可能性がある。 |

既存の`tests/test_paper_order_client.py`は通常価格、スリッページ、残高、価格未設定時の拒否を確認するが、0以下・非有限・古い価格の約定検証は確認できない。

## 必須対応

- `PaperOrderClient`は注文口で欠落(`None`)、0以下、非有限(`NaN`・正負inf)の価格を理由コード付きで拒否し、約定状態を更新しない。注文口は時刻を持たず、秒数のしきい値も設けない。
- 価格拒否は第1弾の`ORDER_REJECTED_*`系判断記録の仕組みに載せる。新規理由コード名は既存の大文字snake-caseに合わせ、案として`ORDER_REJECTED_PAPER_PRICE_MISSING`、`ORDER_REJECTED_PAPER_PRICE_INVALID`を置く。名称・分類の最終確定は実装時に行う。
- 鮮度は秒数でなくEOD既存ルールに揃え、注文直前に板を取り直し、板取得成功・`CurrentPriceTime`がtimezone付きでJST当日・`CurrentPriceStatus`が1または8・価格が正の有限値、のすべてを満たす場合だけ緊急停止の清算注文を行う。
- 緊急停止の鮮度検証失敗時は注文せず、`EOD_LIQUIDATION_UNRESOLVED`相当のERROR記録を行い、失敗銘柄をまとめたSlack critical通知を1回送る。ペーパーでは該当保有は未決済のまま口座状態に残る。
- 拒否時に現金、保有数量、平均取得価格、注文一覧、永続状態のいずれも誤って約定後の値へ進めない。
- 本番のEOD鮮度失敗時の成行注文可否・約定価格記録は、本番移行前の既存タスクに委ね、本タスクで重複実装しない。

## 決定事項

1. **注文口は不正価格を拒否する**: `PaperOrderClient`はNone、0以下、非有限値を注文口で拒否する。EOD freshness検証は秒数しきい値を設けずJST当日・status 1/8・正の有限値で判定する。理由コード名は上記の候補を実装時に確定する。
2. **緊急停止もEODと同じ鮮度検証を行う**: 発注直前にboardを取得し、当日性・status・価格を検証する。失敗銘柄は発注せず未決済として記録し、ERRORログと銘柄まとめのSlack critical通知を1回出す。Paper口座では保有を残す。
3. **ヒストリカル再生を変えない**: `HistoricalBoardClient`が返すシミュレーション時刻に対応した時刻付き価格を使い、実時間の秒数しきい値を適用しない。既存のバックテストv2複数日結果を維持する。

## 売買結果への影響

**変わりうる。** 従来は模擬約定されていた不正・古い価格の注文が拒否され、約定数・損益・残高・保有状態が変わりうる。戦略評価で用いる**40件カウントの起点（トレード回数で戦略を判断する区切り）に影響する**ため、適用日と除外理由を識別できる記録が必要。

## 完了条件

- [x] 買い・売り双方で価格が欠落、0以下、NaN、正負inf、数値以外のとき約定せず、理由コードを記録する。
- [x] 緊急停止で鮮度検証に失敗した場合、発注せずERROR記録と銘柄まとめ通知を1回行い、ペーパー保有を残す。
- [x] 緊急停止で鮮度検証が成功した場合、決済する。status 8の引け後価格も受け入れる。
- [x] freshness検証の当日性、status 1/8、正の有限価格を確認する既存テストを維持する。
- [x] バックテストv2の複数日再生（13日・8注文）の結果を実装前後で確認する。
- [x] 実kabu注文を行うテストを作成しない。
- [x] 手動緊急停止はpaperだけfreshness検証を要求し、liveは従来の非fresh経路を維持する。
- [x] フルテストスイートを実行する。

## 実施記録（2026-10-04）

- PaperOrderClientは注文開始時に価格を検証し、`None`を`ORDER_REJECTED_PAPER_PRICE_MISSING`、0以下・非有限・数値以外を`ORDER_REJECTED_PAPER_PRICE_INVALID`として拒否する。拒否理由は`last_rejection_reason`とERRORログで公開し、UseCaseのDecisionJournalにも記録する。約定状態・注文ID・状態ファイルは更新しない。
- モード判定は`TradingUseCase._is_paper_mode()`へ集約した。手動緊急停止でpaperは既存のfreshness検証を使用し、liveは`require_fresh_price=False`の従来挙動を維持する。
- 複数日再生の実装前基準: 13日、8注文、実現損益 -489.32円（独立再計算一致）。実装後も同値。
- paper価格拒否、緊急停止paper/live分岐、status 8、既存15:20/遅延決済をテストで確認。全テストは完了時の実行記録を参照。

## 関連資料

- [本番EOD決済の鮮度検証と約定価格記録](./task-live-eod-liquidation-and-fill-reconciliation.md)（本番経路。重複範囲は本タスクから除外）
- [ADR-0007](../adr/0007-unify-trading-and-backtest.md)（ヒストリカル再生）