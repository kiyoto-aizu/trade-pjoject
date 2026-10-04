# 改修タスク: 起動時の売買設定値検証

- 起票日: 2026-10-04
- 対象: `src/config/config.py`、関連する設定・起動テスト、`docs/config-reference.md`
- ステータス: **未着手**
- 実施時期: **未定（優先度は別途判断）**
- 関連調査: 2026-10-02「売買部分サイレントスキップ横並び調査」。第1弾は実行時の見送り理由を記録するが、不正な売買パラメータを起動時に網羅検証するものではない。

## 背景

設定値の型変換が行われる項目でも、値の範囲や項目間の関係が起動時に検査されていないものがある。設定の誤りが無注文、損失制限の即時発動、異常な資金配分などとして取引開始後に現れる可能性がある。

## 現行コードで確認した検証

| 項目群 | 現在の検証 | 主な実装 |
|---|---|---|
| モード・認証 | `TRADING_MODE`は`paper` / `live`のみ。liveは`IS_DEMO=false`かつ`ENABLE_LIVE_ORDERING=true`を要求。必須認証・Slack環境変数は欠落時（通常runtime）に`ValueError`。 | `config.py:71-82,350-366` |
| 建玉枠 | `TARGET_POSITIONS <= 0`と`ORDER_UNIT <= 0`は`get_screening_price_cap()`呼出時に`ValueError`。config import時の一括起動検証ではない。 | `config.py:109,159-160` |
| ATR・MarketRegime | ATR期間、CAUTION/DANGER比率、ロット比率、倍率、損益トリガー、観測日数、実現vol期間、ADX閾値等を一部範囲検証。`MarketRegimeThresholds.__post_init__()`もfinite・非負値とdanger/caution順序を検証。 | `config.py:192-241`、`src/domain/market_regime.py:31` |
| Backtest / Paperコスト | fee、slippage、遅延barの非負、order type選択、Paper fee/slippageの非負を検証。 | `config.py:248-263` |
| 価格帯別上限 | `SCREENING_ALTERNATE_PRICE_CAPS`の正値・重複を検証。 | `config.py:287-295` |
| 未検証の売買・資金値 | RSI各閾値・期間・最低終値数、`MAX_ORDER_AMOUNT_PER_TRADE`、`OPERATING_CAPITAL`、`MAX_ORDER_COUNT_PER_DAY`、`DAILY_LOSS_LIMIT_RATIO`、`API_SOFT_LIMIT`等は設定代入時の範囲検証がない。 | `config.py:106-118,142,180-184` |

## 検証候補と現状影響（範囲案。確定仕様ではない）

| 設定項目 | 候補となる許容範囲・関係 | 範囲外値の現在挙動（コード確認） |
|---|---|---|
| `RSI_PERIOD` | 整数`>= 1` | 0以下でもconfig importは通る。RSI計算時に除算エラー等が起き得る。 |
| `RSI_ENTRY_THRESHOLD`, `RSI_ENTRY_THRESHOLD_CAUTION`, `RSI_EXIT_THRESHOLD` | 各`0..100`。`RSI_EXIT_THRESHOLD < RSI_ENTRY_THRESHOLD`、CAUTION閾値とNORMAL閾値の順序は要判断 | 値域・項目間関係をconfigでは検証しない。条件が常時成立しない、または意図しない方向の発注条件となり得る。 |
| `RSI_MINIMUM_CLOSES` | 整数`>= max(5, RSI_PERIOD + 1)`を候補とする | 非正値・期間との不足関係をconfigでは検証しない。呼出し側の`max()`や`calculate_rsi()`側の本数判定に委ねられる。 |
| `TARGET_POSITIONS` | 整数`>= 1` | config import時は通る。`get_screening_price_cap()`呼出時に0以下を拒否するが、起動経路により検出時点が異なる。 |
| `MAX_ORDER_AMOUNT_PER_TRADE`, `OPERATING_CAPITAL`, `API_SOFT_LIMIT` | 有限値かつ`> 0`を候補とする | configでは範囲検証しない。負額等は予算計算・数量0・注文拒否などへ波及し得る。 |
| `MAX_ORDER_COUNT_PER_DAY` | 整数`>= 1`を候補とする | configでは検証しない。負値なら日次上限判定が意図せず新規買いを止め得る。 |
| `DAILY_LOSS_LIMIT_RATIO` | 有限値`0 < ratio <= 1`を候補とする。運用で1を超える値を許すかは要判断 | configでは検証しない。負値・0・過大値は停止閾値の意味を変える。 |

上表は現状挙動とレビュー用の候補範囲であり、採用する許容値・項目間制約を確定したものではない。非有限値（NaN/inf）を含めるか、整数変換前後でどの形式を許すかも要判断。

## 必須対応

- 売買・サイジングに影響する設定項目と相互制約について、許容範囲、範囲外時の結果、検証タイミングを一覧化する。
- coding-guidelines §3のfail-fast方針と整合し、無効な値で売買処理が開始されないことを保証する。起動時検証にする場合は、起動入口で必要な全設定が検証されることをテストする。
- 既存のATR・Backtest・Paper等の検証との重複や一貫性を整理し、同じ設定の検証結果が使用経路ごとに変わらないようにする。
- 設定値そのものや秘密値を例外・ログへ出さない。エラーは設定名と不正理由を特定できる形にする。

## 要判断・運用確認

1. RSI三閾値の順序制約、RSI期間と最低終値数の関係、損失比率の上限をどこまで固定するか。
2. 正の有限値などの基本制約と、戦略上の推奨域を分けて扱うか。
3. `.env`実値が提案範囲に適合するかは未確認。運用者が各環境でローカル確認し、秘密情報そのものは共有せず、項目ごとの適合/不適合だけを確認する必要がある。
4. 不適合が見つかった際に値を修正するか、許容範囲を見直すかを運用判断する。`.env`を本タスクで読み取ったり変更したりしない。

## 売買結果への影響

**変わりうる。** 無効値を起動時に拒否すると、従来は起動後に無注文・数量0・早期停止となっていた運用が起動不可になる。現在の運用値が範囲外なら取引開始日が変わり、約定数・損益および**40件カウントの起点**に影響する。

## 完了条件

- 既存検証項目と今回対象の未検証項目を整理した設定仕様表がある。
- 有効境界値・境界外値・NaN/inf・相互制約違反を含む単体テストで、設定名とエラー内容を確認する。
- 無効設定時に各entrypointのUseCase・PaperOrderClient・注文送信が開始されないことをテストする。
- テストは環境変数を明示的に隔離し、実`.env`の秘密値やユーザー設定を読み書きしない。
- 実kabu注文を発生させるテストは行わない。

## 関連資料

- [coding-guidelines.md §3](../coding-guidelines.md)（秘密情報・起動時fail-fast）
- [設定項目リファレンス](../config-reference.md)（生成済み環境変数一覧）