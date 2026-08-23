# trade-pjoject 詳細設計書 ②フィルタ機能

対象: ①のスクリーニング結果（30〜50銘柄）を入力とし、当日のトレードループ開始前に **10銘柄** へ絞り込む機能。
担当ユースケース: `application/filtering_usecase.py`

---

## 1. 実行方式・タイミング

- **独立起動**: `entrypoints/run_filtering.py` を専用エントリポイントとして分離する（①スクリーニングと同じ構成に揃え、障害時の切り分け・単体再実行をしやすくする）
- **実行タイミング: 当日9:30頃に確定。** 寄り付き直後（9:00〜9:05頃）は値動き・出来高が特に荒くノイズを拾いやすいため、寄り付きの荒さがやや落ち着く9:30頃に実行する
- 実行順序:

```
Start(朝の起動)
 → ①API接続&トークン取得（run_trading.py側）
 → 【本設計書】run_filtering.py 実行（前日スクリーニング結果30〜50銘柄 → 当日10銘柄）
 → ②資産・保有株確認（run_trading.py側）
 → メイン監視ループ開始（③〜、10銘柄を対象）
```

- 前提: 当日の場中（寄り付き後）に実行するため、`GET /ranking` のクリア時間帯（7:53〜9:00過ぎ）は本機能では問題にならない（`/ranking`は①でのみ使用）
- `run_filtering.py`は`run_trading.py`とは別プロセスのため、③トレードループ機能はループ開始前に`FilteringResultRepository.load_latest()`で結果を読み込む（プロセス間の受け渡しはファイル経由）

---

## 2. 処理フロー

```
① 前日スクリーニング結果の読み込み（30〜50銘柄）
  → infrastructure/persistence/screening_result_repository.py（①の成果物を再利用）
② 各銘柄の当日出来高・平均出来高を取得
  → GET /board/{symbol}（当日累計出来高 TradingVolume）
  → infrastructure/market_data/yahoo_finance_client.py（過去N日の平均出来高）
③ 出来高急増率でスコアリング・上位10件抽出
  → domain/rules.py の純粋関数
④ 結果を永続化・トレードループへ引き渡し
  → infrastructure/persistence/filtering_result_repository.py
```

---

## 3. 出来高急増率の算出について（重要な制約）

kabuステーションAPIはリアルタイム取引APIであり、**過去の平均出来高を返すエンドポイントを持たない**
（`/board/{symbol}`は当日の`TradingVolume`のみ）。
そのため、出来高急増率の基準値（平均出来高）は **`infrastructure/market_data/yahoo_finance_client.py` 経由でYahoo Finance等の外部データから取得**する。

```
出来高急増率 = 当日累計出来高（kabu /board） ÷ 過去N日平均出来高（Yahoo Finance）
```

- kabu側とYahoo Finance側で銘柄コードの表記が異なる場合がある（例: `.T`サフィックス等）ため、
  `infrastructure/market_data/yahoo_finance_client.py`側で銘柄コード変換を吸収する
- Yahoo Finance側の取得に失敗した銘柄は、基準値が不明なためスコアリング対象から除外する（推測値で補わない）

---

## 4. レイヤー別設計

### application/filtering_usecase.py
- `FilteringUseCase.execute() -> FilteringResult`
- 責務: ①〜④の呼び出し順序を制御。ロジックは持たない。
- 依存: `ScreeningResultRepository`（①の成果物読込）, `BoardRepository`, `YahooFinanceClient`（平均出来高取得）, `filtering_rules`（domain）, `FilteringResultRepository`

### infrastructure/market_data/yahoo_finance_client.py（既存想定・追加実装）
- `get_average_volume(symbol: str, days: int = 20) -> float | None`
- 過去20営業日（約1ヶ月）の平均出来高を返す。取得失敗時は`None`
- 銘柄コード変換ルール: kabu側の4桁コードに`.T`を付与してYahoo Finance用コードに変換する（例: `7203` → `7203.T`）

### domain/rules.py 追加関数
- `calculate_volume_surge_ratio(today_volume: float, average_volume: float) -> float`
  - 出来高急増率 = 当日出来高 ÷ 平均出来高（純粋関数）
- `select_top_n_by_surge_ratio(scored: list[ScoredCandidate], n: int = 10) -> list[str]`
  - 出来高急増率が高い順に上位10件（固定）を抽出

### domain/models.py 追加モデル
| モデル | フィールド | 用途 |
|---|---|---|
| `ScoredCandidate` | symbol, today_volume, average_volume, surge_ratio | ③の中間結果 |
| `FilteringResult` | date, symbols(list[str]), generated_at | ④の永続化対象・③(トレードループ)の入力 |

### infrastructure/persistence/filtering_result_repository.py（新規）
- `save(result: FilteringResult) -> None`
- `load_latest() -> FilteringResult | None`
- 用途: `TradingUseCase`（③トレードループ機能）が監視対象銘柄リストとしてここから読み込む

---

## 5. 異常系設計

| ケース | 対応 |
|---|---|
| ①のスクリーニング結果ファイルが存在しない（前日実行失敗等） | フィルタ処理をスキップし、エラー通知。`FilteringResult`は「対象0件」として保存し、③トレードループ側は安全に待機（推測で銘柄リストを補わない） |
| `GET /board/{symbol}` が一部銘柄で失敗 | 当該銘柄はスコアリング対象から除外（他銘柄の処理は継続） |
| Yahoo Financeの平均出来高取得が一部銘柄で失敗 | 当該銘柄はスコアリング対象から除外（基準値不明のため急増率を計算しない） |
| 絞り込み後の候補が10件に満たない | 取得できた分だけで継続（警告ログのみ、処理は止めない） |

---

## 6. すり合わせ済み事項（2026-08-15）

- フィルタ指標: 出来高急増率のみ（当日出来高 ÷ 過去平均出来高）
- 平均出来高の取得元: Yahoo Finance等の外部API（`infrastructure/market_data/yahoo_finance_client.py`）
- 平均出来高の算出期間: 20営業日（約1ヶ月）
- 銘柄コード変換ルール: 「4桁+.T」（例: `7203` → `7203.T`）
- 起動方式: 独立起動（`entrypoints/run_filtering.py`を別プロセスで実行、結果はファイル経由で③に受け渡す）
- 実行タイミング: 当日9:30頃
- 対象銘柄数: 10件固定

## 7. 実行スケジュール（cron設定例）

```cron
# 平日9:30に当日フィルタを実行
30 9 * * 1-5 /usr/bin/python3 /path/to/trade-pjoject/entrypoints/run_filtering.py >> /var/log/trade-pjoject/filtering.log 2>&1
```

- `run_trading.py`（③トレードループ）は、cronで別途9:30以降の時刻に起動するか、`run_filtering.py`の完了を待ってから起動する仕組みが必要（起動順序の制御方法は03側の検討事項とする）
- 休場日の扱いは①スクリーニングと同様、`FilteringResult`が「対象0件」で保存されるだけで異常終了はしない設計とする

## 8. 関連設計書

- `01-screening-design.md`（①前日スクリーニング、本機能の入力元）
- `03-trading-loop-design.md`（③トレードループ、本機能の出力先）
