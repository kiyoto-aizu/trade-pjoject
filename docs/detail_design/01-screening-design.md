# trade-pjoject 詳細設計書 ①スクリーニング機能

対象: 前日に、翌営業日のトレード対象候補を **30〜50銘柄** に絞り込む機能。
担当ユースケース: `application/screening_usecase.py`（[coding-guidelines.md](../architecture/coding-guidelines.md)記載の構成に対応）

---

## 1. 実行タイミング

- `src/config/task_schedule.py`の予定は平日15:35。翌営業日向けの上場銘柄マスタとYahoo日足によるランキングを作る。
- 現行の通常経路はkabuステーション`GET /ranking`ではなく、`HistoricalRankingRepository`が対象日の上場銘柄ごとに日足データを読み、売買代金・値上がり率を算出する。したがって、旧記載の7:53頃の`/ranking`データクリア制約は通常経路には適用されない。
- `run_screening.py`は市場・規制APIの照会結果を`ScreeningApiCheckRepository`で日付単位に記録・再利用する。通常実行の出力は`SCREENING_RESULT_DIRECTORY`、追加価格帯は別ディレクトリに保存する。
- 価格上限を優先したユニバース選定への移行は[ADR-0001](../adr/0001-price-first-universe-selection.md)を参照（設計書内で決定内容を重複記載しない）。

---

## 2. 処理フロー

```
① 対象日付の上場銘柄と日足からランキングを生成
  → ListedSecurityRepository + HistoricalRankingRepository
  → Yahoo日足から売買代金（終値×出来高）・値上がり率を算出
② 価格上限を先に適用
  → domain/rules.py の`filter_candidates_by_price()`
  → `get_screening_price_cap()`で本番上限を決め、通常結果を保存
③ 規制・市場チェック
  → `ScreeningApiCheckRepository`が対象日ごとの市場・規制API結果を保存・再利用
  → `SCREENING_BATCH_SIZE`単位で銘柄登録・照会・解除
④ 統合順位から候補を選び、監査情報とともに永続化
  → `ScreeningResultRepository`、監査行は`ScreeningAuditEntry`
⑤ Slack dailyへ完了件数・除外内訳・代表銘柄を通知（3.5節参照）
⑥ 通常の価格上限とは別に、`SCREENING_ALTERNATE_PRICE_CAPS`（既定450円・900円）の価格帯別結果を作成
  → `SCREENING_PRICE_BAND_RESULT_ROOT/<価格上限>/`
  → 追加価格帯は規制・市場情報が未確認でも候補を残す検証用経路（`keep_unconfirmed=True`）
```

> （2026-09-17更新）当初は「銘柄選定では株価による除外を行わない」方針だったが、1銘柄あたりの
> 想定予算（運用資金÷目標保有数、発注額上限で頭打ち）で1単元も買えない極端な高額銘柄を候補に
> 残しても②③で無駄になるだけのため、価格上限フィルタ（①’）を追加した。発注額・数量の制限は
> 引き続き取引ユースケースのキルスイッチでも管理する（多重の安全網という位置づけ）。
> `/board` は未登録銘柄の照会時にAPI登録銘柄枠を消費するため、スクリーニングでは使用しない。

---

## 3. レイヤー別設計

### application/screening_usecase.py
- `ScreeningUseCase.execute() -> ScreeningResult`
- 責務: ①〜⑤の呼び出し順序を制御する司令塔。ロジックは持たない。
- 依存: `RankingRepository`, `RegulationRepository`, `PrimaryExchangeRepository`, `screening_rules`（domain）, `ScreeningResultRepository`, `Notifier`
- **通知用サマリの組み立てもここで行う**（`ExclusionResult`の除外件数、`limit_candidates`前の統合済みランキングから上位数銘柄のrank/valueを抜き出して`Notifier`に渡す）。永続化する`ScreeningResult`自体には持たせない（3.5節参照）
- **（実装差分）`execute(target_date: date | None = None)`**: バックテスト用に過去日付を指定してランキング・規制情報を取得し直す「リプレイ」に対応する。`target_date=None`（通常運用）の場合は従来通り当日実行を前提とした呼び出しを行い、指定時のみ`RankingRepository`・`RegulationRepository`に`target_date`を追加で渡す
- **（実装差分）`batch_started` / `batch_finished`フック**: `②規制・除外条件の確認`はkabuステーションAPIの銘柄登録枠を消費するため、`config.SCREENING_BATCH_SIZE`件ずつのバッチに分割して処理する。各バッチの前後で`batch_started(batch, batch_number)` / `batch_finished(batch, batch_number)`を呼び出し、`entrypoints/run_screening.py`側でkabuステーションAPIへの銘柄登録(`register_symbols`)・解除(`unregister_all`)に接続する。フックが失敗（`False`を返す）した場合はそのバッチで処理を中断する

### market_data/historical_ranking_repository.py（現行）
- `HistoricalRankingRepository.get_ranking()`は`ListedSecurityRepository.load_for_date()`で対象日に上場している銘柄を列挙し、Yahoo日足からランキングを作る。
- 売買代金は`close × volume`、値上がり率は`close / previous_close - 1`。通常実行では`run_screening.py`がこのリポジトリを使用する。
- kabu市場・規制APIの取得結果は`ScreeningApiCheckRepository`で対象日JSONにキャッシュする。これはランキング取得用ではなく、API照会の再利用・監査用。

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
- **（実装差分）`filter_candidates_by_price(candidates: list[str], price_by_symbol: dict[str, float], price_cap: float) -> PriceFilterResult`**
  - ①’の価格上限フィルタ本体。価格が`price_cap`を超える銘柄、および価格が取得できなかった銘柄（`None`または0以下）を除外する
  - `price_cap`は呼び出し側（`ScreeningUseCase`）が`config.get_screening_price_cap()`で算出して渡す純粋関数。しきい値の計算式自体はconfig側の責務とし、domain側は「渡された上限値で振り分けるだけ」に留める
  - 価格上限超過と価格不明を別カウントで返す（通知の除外内訳・監査ログの理由分けに使うため）
- `exclude_by_regulation(candidates: list[str], regulations: dict[str, Regulation]) -> ExclusionResult`
  - 規制銘柄・対象外取引所（地方取引所単独上場銘柄）の銘柄を除外
  - **通知の除外内訳表示のため、単なる`list[str]`ではなく `ExclusionResult`（残った銘柄 + 理由別の除外件数）を返す**（下記モデル参照）
  - 理由の切り分け: 信用規制・値幅制限による除外は`reason="regulation"`、地方取引所単独上場による除外は`reason="exchange"`としてカウントを分ける
- `limit_candidates(candidates: list[str], min_count: int = 30, max_count: int = 50) -> list[str]`
  - 最終的に30〜50件に丸める（多すぎる場合はスコア上位から、少なすぎる場合は警告ログ）

> 流動性フィルタ（売買代金下限による追加絞り込み）は初回リリースでは導入しない。
> `Type=4:売買代金`ランキング自体が一定の流動性を担保するため、まずはこれで運用し、
> 実績が溜まってから下限値の要否・具体的な閾値を再検討する。

### domain/models.py 追加モデル
| モデル | フィールド | 用途 |
|---|---|---|
| `RankingEntry` | symbol, rank, value, ranking_type, current_price | ①の取得結果・②の判定材料 |
| `Regulation` | symbol, is_restricted, reason | ②の判定材料 |
| `PriceFilterResult`（実装差分） | remaining(list[str]), excluded_by_price_count, excluded_missing_price_count | `filter_candidates_by_price()`の出力・通知の除外内訳と監査ログ理由の元データ |
| `ExclusionResult` | remaining(list[str]), excluded_by_regulation_count, excluded_by_exchange_count | `exclude_by_regulation()`の出力・通知の除外内訳の元データ |
| `ScreeningResult` | date, symbols(list[str]), generated_at, audit_entries(list[`ScreeningAuditEntry`]) | ⑤の永続化対象・②の入力。`audit_entries`は監査目的で追加した実装差分（2026-09-04追記、5節参照）で、全候補のランキング・規制・採用判定情報を保持する |
| `ScreeningAuditEntry` | symbol, turnover_rank, turnover_value, price_gain_rank, price_gain_value, total_rank, primary_exchange, is_restricted, restriction_reason, selected | 監査ログ用。なぜその銘柄が採用/除外されたかを後から再現するための全候補分の記録 |

`src/config/config.py`に以下の設定項目を追加する:

| 設定項目 | 既定値 | 用途 |
|---|---|---|
| `SCREENING_PRICE_MARGIN`（実装差分） | 0.9 | `get_screening_price_cap()`の安全マージン。1単元分ぴったりの予算だと発注時の株価変動で買えなくなる恐れがあるため、想定予算の90%までを上限とする |
| `SCREENING_BATCH_SIZE`（実装差分） | 50 | ②規制・除外条件の確認をバッチ処理する際の1バッチあたりの銘柄数（銘柄登録枠の制約に対応） |

`get_screening_price_cap()`（実装差分、config.py）: `min(OPERATING_CAPITAL / TARGET_POSITIONS, MAX_ORDER_AMOUNT_PER_TRADE) / ORDER_UNIT * SCREENING_PRICE_MARGIN`で1単元あたりの上限株価を算出する。取引ユースケース側の1銘柄あたり予算枠（③の設計書参照）と同じ考え方を、前日時点の設定値ベースで先取りして使っている

### infrastructure/persistence/screening_result_repository.py（新規）
- `save(result: ScreeningResult) -> None`
- `load_latest() -> ScreeningResult | None`
- 保存先: 日付付きファイル（例: `data/screening/2026-08-15.json`）
- 用途: ②のフィルタ機能が翌朝この結果を読み込んで使用する

### 3.5 通知内容の設計（Slack dailyチャンネル）

**方針**: 監視も兼ねる。1〜2行に収め、銘柄コードの羅列はしない。

```
スクリーニング完了: 42銘柄（候補70件中、価格上限5件・価格不明1件・規制3件・地方取引所2件を除外）
上位: 285A(値上がり率+18.2%) / 593A(売買代金12.4億) / 1234(値上がり率+15.1%)
```

- **1行目: 件数 + 除外内訳**
  - 最終件数（`limit_candidates`後の件数、`ScreeningResult.symbols`の件数と一致）
  - `候補◯件中` = `merge_ranking_candidates()`直後（除外・丸め込み前）の件数
  - **（2026-09-17更新）**`価格上限◯件` = `PriceFilterResult.excluded_by_price_count`、`価格不明◯件` = `PriceFilterResult.excluded_missing_price_count`
  - `規制◯件` = `ExclusionResult.excluded_by_regulation_count`
  - `地方取引所◯件` = `ExclusionResult.excluded_by_exchange_count`
  - 用途: 除外件数が普段と桁違いに多い/少ない日に気づける（監視目的）
- **2行目: 上位銘柄（3〜5件程度、代表値付き）**
  - 各銘柄について、売買代金ランキング・値上がり率ランキングのうち**より高順位だった方**の種別と実際の値を添える
  - 銘柄コードだけの羅列を避け、「なぜ選ばれたか」が一目でわかるようにする
- 候補が多い日は上位行を優先し、残りは1行目の件数のみで足りるものとする
- 通知失敗は本処理（絞り込み・永続化）に影響させない（§4の異常系表に準ずる）

---

## 4. 異常系設計

| ケース | 対応 |
|---|---|
| 対象日の上場銘柄または日足が不足しランキングが空 | `ScreeningUseCase.execute()`が処理を中断する。推測値で補わない。 |
| ランキング応答の`CurrentPrice`が欠損 | 当該銘柄は安全側に倒して除外（候補に残さない。高額かどうか判定不能なため） |
| 規制情報API取得失敗 | 当該銘柄は安全側に倒して除外（候補に残さない） |
| 絞り込み後の候補が30件未満 | 警告ログを出し、処理は継続（発注可否は②③側の責務。①は「候補を出す」までが責務） |
| 前日実行が失敗し当日候補が存在しない | ②のフィルタ機能側でも「候補ファイルが存在しない場合は処理をスキップし通知する」ガードを持つ（②の設計書側に記載） |

---

## 5. すり合わせ済み事項（2026-08-15）

- ランキング種別: 売買代金 + 値上がり率（当初のkabu `/ranking` Type=4/1方式は上場銘柄マスタ+日足による自前計算へ置き換え済み、2026-10-04）
- 流動性フィルタ: 初回リリースでは導入しない（実績を見てから再検討）
- 実行タイミング: 前日大引け直後（15:35頃）
- 銘柄統合ロジック: 順位合算方式（両ランキングの合計順位が小さい順）
- 除外する対象外市場: `PrimaryExchange` が `3(名証)` `5(福証)` `6(札証)` の地方取引所単独上場銘柄のみ（東証はすべて対象内）
- 株価上限: **（2026-09-17更新）**当初は「銘柄選定では設けない」としていたが、1単元も買えない極端な高額銘柄を候補に残す無駄を避けるため、`config.get_screening_price_cap()`による動的な価格上限フィルタ（①’）を追加した。発注額上限・注文数量による制御（取引ユースケース側）は引き続き併用する
- 通知内容: 監視も兼ねる方針で確定。1行目「最終件数＋候補件数＋除外内訳（高額/規制/地方取引所）」、2行目「上位3〜5銘柄＋代表値（順位で勝った方のランキング種別と値）」の2行構成（3.5節参照）
- 永続化モデル(`ScreeningResult`)は変更しない。通知用の詳細情報（順位・値・除外内訳）は`ScreeningUseCase`内で都度組み立てて`Notifier`に渡す
- **（2026-09-04追記）実装レビューを踏まえ、監査目的で`ScreeningResult.audit_entries`（`ScreeningAuditEntry`のリスト）を例外的に追加した。上記の「永続化モデルは変更しない」方針からの逸脱だが、後から採用判定の根拠を追えるようにするための意図的な差分として本節に記録する**
- **（2026-09-17追記）実装レビューを踏まえ、以下2点をドキュメント化した（コードは既存、設計書側が追いついていなかった差分）**
  - `ScreeningUseCase.execute(target_date=None)`: バックテスト用の過去日付リプレイ対応（3節参照）
  - `batch_started`/`batch_finished`フック: ②の規制情報取得を銘柄登録枠の制約に合わせてバッチ処理するための仕組み（3節参照）

- **（2026-10-04追記）通常経路はkabu `/ranking`取得から上場銘柄マスタ+Yahoo日足による価格上限先行選定へ置き換え済み**。過去の`/ranking`前提は履歴として保持し、実装はADR-0001・本節の現行フローを参照。
- **（2026-10-04追記）通常価格上限とは別に、`SCREENING_ALTERNATE_PRICE_CAPS`で価格帯別検証結果を保存する。市場・規制API結果は`SCREENING_API_CHECK_DIRECTORY`に対象日単位で保存・再利用する。**

## 6. 実行スケジュール

`src/config/task_schedule.py`は取引日カレンダーに応じた予定表を返し、スクリーニングを平日15:35に設定している。実際のプロセス起動はOSスケジューラ等が行う。

| タスク | 予定 |
|---|---|
| スクリーニング | 平日15:35（翌営業日向け） |

## 7. 残る確認事項

1. リポジトリ内の`task_schedule.py`は予定を返すだけで、OSスケジューラへの登録状態はコードから確認できない。運用環境の起動設定は別途確認する。
2. 価格帯別結果を通常の取引候補へ採用するかは、診断データ蓄積後に判断する。

## 8. 設定項目
設定項目は[config-reference.md](../reference/config-reference.md)を参照。
