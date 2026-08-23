# trade-pjoject 詳細設計書 ①スクリーニング機能

対象: 前日に、翌営業日のトレード対象候補を **30〜50銘柄** に絞り込む機能。
担当ユースケース: `application/screening_usecase.py`（coding-guidelines.md記載の構成に対応）

---

## 1. 実行タイミングの制約（重要）

kabuステーションAPIの `GET /ranking` は「kabuステーションが保持している**当日**のデータ」を返す仕様で、
株価情報ランキングは **平日7:53頃〜9:00過ぎ頃にデータがクリアされる**（yaml記載）。

→ そのため本機能は、**前日の大引け後〜当日7:53より前**の時間帯に実行する必要がある。
この時間帯であれば「前日」の値上がり率・売買代金等のランキングが「当日データ」として取得できる。
7:53以降に実行するとランキングが空レスポンスになるため、スケジューラ側で実行時刻を固定するか、
実行前に空レスポンスを検知して即エラー終了する安全策が必要。

- **実行時刻: 前日大引け直後（15:35頃）に確定。**
  - 大引け後のランキングは翌朝7:53のクリアまで内容が変わらないため、15:35以降であればいつ実行しても結果は同じ。
  - 最も「前日の結果」に忠実で、夜間バッチ等の他処理に巻き込まれるリスクも避けられるため、この時刻を採用する。
- `entrypoints/run_screening.py` を専用エントリポイントとして分離（coding-guidelines.md 5.3節「スケジュール実行と手動実行で起動条件が異なる場合」に該当）

---

## 2. 処理フロー

```
① ランキング取得（2種別を組み合わせ）
  → GET /ranking （Type=4:売買代金, Type=1:値上がり率 の2種類を取得）
② 規制・除外条件の確認
  → GET /regulations/{symbol}（値幅制限・信用規制など）
  → GET /primaryexchange/{symbol}（取引所確認、対象外市場の除外）
③ ドメインルールでスコアリング・絞り込み
  → domain/rules.py の純粋関数でランキング結果を統合評価
④ 30〜50銘柄に絞って永続化
  → infrastructure/persistence/screening_result_repository.py
⑤ 結果を通知（任意）
  → infrastructure/notification/line_notify_client.py
```

---

## 3. レイヤー別設計

### application/screening_usecase.py
- `ScreeningUseCase.execute() -> ScreeningResult`
- 責務: ①〜⑤の呼び出し順序を制御する司令塔。ロジックは持たない。
- 依存: `RankingRepository`, `RegulationRepository`, `PrimaryExchangeRepository`, `screening_rules`（domain）, `ScreeningResultRepository`, `Notifier`

### infrastructure/kabu/ranking_repository.py（新規）
- `get_ranking(ranking_type: RankingType) -> list[RankingEntry]`
- API: `GET /ranking`（`Type`パラメータをEnum化。coding-guidelines.md 2.3節「マジックストリングはEnum化」に準拠）
- `RankingType.TURNOVER`（Type=4: 売買代金）と `RankingType.PRICE_GAIN`（Type=1: 値上がり率）の2種類を取得し、`ScreeningUseCase`側で結合する

### infrastructure/kabu/regulation_repository.py（新規）
- `get_regulation(symbol: str) -> Regulation`
- API: `GET /regulations/{symbol}`
- 用途: 値幅制限中・信用規制中などトレード対象として不適な銘柄を除外する判定材料

### infrastructure/kabu/primaryexchange_repository.py（新規）
- `get_primary_exchange(symbol: str) -> int`
- API: `GET /primaryexchange/{symbol}`
- `PrimaryExchange` の定義値は `1:東証, 3:名証, 5:福証, 6:札証`
- **除外対象は「地方取引所単独上場銘柄のみ」= `PrimaryExchange` が `3(名証)`, `5(福証)`, `6(札証)` のいずれかの銘柄**。`1(東証)`はそのまま候補に残す

### domain/rules.py 追加関数（純粋関数、外部依存ゼロ）
- `merge_ranking_candidates(turnover_ranking: list[RankingEntry], price_gain_ranking: list[RankingEntry]) -> list[str]`
  - **順位合算方式**: 各銘柄について「売買代金ランキングの順位＋値上がり率ランキングの順位」を合計し、合計順位が小さい順に採用する
  - 片方のランキングにしか出ていない銘柄は、出ていない側の順位を「ランキング対象外の下限値（例: 取得件数+1）」として計算し、著しく不利な扱いにする
- `exclude_by_regulation(candidates: list[str], regulations: dict[str, Regulation]) -> list[str]`
  - 規制銘柄・対象外取引所（地方取引所単独上場銘柄）の銘柄を除外
- `limit_candidates(candidates: list[str], min_count: int = 30, max_count: int = 50) -> list[str]`
  - 最終的に30〜50件に丸める（多すぎる場合はスコア上位から、少なすぎる場合は警告ログ）

> 流動性フィルタ（売買代金下限による追加絞り込み）は初回リリースでは導入しない。
> `Type=4:売買代金`ランキング自体が一定の流動性を担保するため、まずはこれで運用し、
> 実績が溜まってから下限値の要否・具体的な閾値を再検討する。

### domain/models.py 追加モデル
| モデル | フィールド | 用途 |
|---|---|---|
| `RankingEntry` | symbol, rank, value, ranking_type | ①の取得結果 |
| `Regulation` | symbol, is_restricted, reason | ②の判定材料 |
| `ScreeningResult` | date, symbols(list[str]), generated_at | ④の永続化対象・②の入力 |

### infrastructure/persistence/screening_result_repository.py（新規）
- `save(result: ScreeningResult) -> None`
- `load_latest() -> ScreeningResult | None`
- 保存先: 日付付きファイル（例: `data/screening/2026-08-15.json`）
- 用途: ②のフィルタ機能が翌朝この結果を読み込んで使用する

---

## 4. 異常系設計

| ケース | 対応 |
|---|---|
| `/ranking` が空レスポンス（7:53以降に実行してしまった等） | 処理を中断し、推測値で補わず即エラー終了・通知（coding-guidelines.md 3.3節） |
| 規制情報API取得失敗 | 当該銘柄は安全側に倒して除外（候補に残さない） |
| 絞り込み後の候補が30件未満 | 警告ログを出し、処理は継続（発注可否は②③側の責務。①は「候補を出す」までが責務） |
| 前日実行が失敗し当日候補が存在しない | ②のフィルタ機能側でも「候補ファイルが存在しない場合は処理をスキップし通知する」ガードを持つ（②の設計書側に記載） |

---

## 5. すり合わせ済み事項（2026-08-15）

- ランキング種別: 売買代金（Type=4） + 値上がり率（Type=1）の組み合わせ
- 流動性フィルタ: 初回リリースでは導入しない（実績を見てから再検討）
- 実行タイミング: 前日大引け直後（15:35頃）
- 銘柄統合ロジック: 順位合算方式（両ランキングの合計順位が小さい順）
- 除外する対象外市場: `PrimaryExchange` が `3(名証)` `5(福証)` `6(札証)` の地方取引所単独上場銘柄のみ（東証はすべて対象内）

## 6. 実行スケジュール（cron設定例）

```cron
# 平日15:35に前日スクリーニングを実行
35 15 * * 1-5 /usr/bin/python3 /path/to/trade-pjoject/entrypoints/run_screening.py >> /var/log/trade-pjoject/screening.log 2>&1
```

- 祝日・大納会/大発会等の非営業日はcron側では判定しない。`run_screening.py`側で「取引所が休場だった場合、`/ranking`が空になる」ことを想定し、§4の異常系（空レスポンス検知→即エラー終了）で吸収する
- サーバー時刻とkabuステーション（証券会社サーバー）側の時刻ズレを考慮し、実運用では15:35ちょうどではなく数分後（例: 15:40）に設定する余地もある。まずは15:35で開始し、実運用で問題があれば調整する

## 7. 残る検討事項（実装フェーズで対応）

これらは設計方針としては確定済みで、実装時の具体的なパラメータ調整のみが残る。

1. cron実行時刻を15:35のまま運用するか、余裕を見て数分ずらすか（§6参照、実運用データを見て判断）
2. 休場日の扱い（祝日カレンダーとの連携要否）
