# trade-pjoject 詳細設計書 ③トレードループ機能

対象: ②のフィルタ結果（10銘柄固定）を対象として、場中に監視・判定・発注を繰り返すメインループ。
flow.mdの③〜⑨に相当。担当ユースケース: `application/trading_usecase.py`

※①スクリーニング設計書・②フィルタ設計書と対になる。①②で対象銘柄が確定している前提で本設計書はループ本体を扱う。

---

## 1. 起動シーケンス・他機能との連携

```
（前日）15:35頃  run_screening.py 実行（①、対象30〜50銘柄を保存）
（当日）09:30頃  run_filtering.py 実行（②、対象10銘柄に絞り保存）
（当日）09:35頃  run_trading.py   実行（③、本設計書）
```

- ①②③はすべて別プロセス・別cronジョブとして起動する（**cronで時刻をずらす方式に確定**。完了待ち合わせの仕組みは作らない）
- `run_trading.py`は起動時に`FilteringResultRepository.load_latest()`で対象銘柄リスト（10件）を読み込む
- **時刻のマージンとして、②(9:30)と③(9:35)の間に5分の余裕**を持たせる。②の処理が9:35までに終わらない事態に備え、③側は「フィルタ結果の`generated_at`が当日日付でない場合は起動を中止し通知する」ガードを持つ（前日の古い結果のまま動き出さないための安全策）
- 対象銘柄が0件（②が失敗・未実行）の場合も同様にループを開始せず通知して終了する

---

## 2. 処理フロー詳細（flow.md ③〜⑨）

### ③ ターゲット銘柄の現在値を取得
- **担当**: `infrastructure/kabu/board_repository.py`（`get_current_board(symbol)`）
- **API**: `GET /board/{symbol}`
- **対象**: ②の10銘柄（**1日を通して固定**。ループ中に再フィルタして差し替える運用はしない）
- **異常系**: `board`が`None`または`current_price`取得不可の場合、当該銘柄のみ評価をスキップ（推測値フォールバック禁止）

### ④ 売買条件の判定
- **担当**: `domain/rules.py`（純粋関数）
- **入力**: `Board`（③）、銘柄ごとの`PriceLimit`/判定パラメータ
- **出力**: `Signal | None`（`SignalType`: BUY/SELL/NONE）
- **テスト観点**: 境界値、データ不足時のスキップ

### ⑤ 発注セーフティチェック
- **担当**: `domain/rules.py`
- **チェック項目**:
  - 予算（**発注のたびに`GET /wallet/cash`を再取得**し、最新の現金残高で判定。起動時キャッシュは使わない）
  - 当日、同一銘柄・同一方向の注文が既に存在しないか（`OrderHistoryRepository`参照）
  - 保有がない銘柄への売り注文でないか（**発注のたびに`GET /positions`を再取得**し最新の保有状況で判定）
  - キルスイッチ条件（**発注上限額はapisoftlimitより厳しい固定額をアプリ側で設定**・**1日の最大発注回数は10回まで**・**1日の想定損失上限は運用資金の一定割合**で設定）を超えていないか
- **NG時**: ⑦へ直行
- **設計上の注意**: 予算・保有を毎回API取得するため、⑤の実行はAPIコールが2回（wallet/cash, positions）増える。ループ間隔（⑦の60秒スリープ）に対して十分許容範囲だが、実装時にレート制限（`/apisoftlimit`）に抵触しないか確認する

### ⑥ 証券会社APIへ注文送信
- **担当**: `infrastructure/kabu/order_repository.py`（`send_order(order: Order)`）
- **API**: `POST /sendorder`
- **副作用**: 発注結果を`OrderHistoryRepository`へ永続化
- **監査ログ**: いつ・どの銘柄を・いくらで・④⑤の何を根拠に発注したかを記録
- **異常系**: API失敗・タイムアウト時は⑦へ抜ける（発注済み扱いにしない）

### ⑦ 指定時間スリープ
- **担当**: `application/trading_usecase.py`（ループ制御。ビジネスロジックではないためdomainに置かない）
- **処理**: 60秒スリープ（`config/settings.py`で設定値化）

### ⑧ 時刻チェック
- **担当**: `domain/rules.py`（`is_market_closed(now: time) -> bool`）
- **分岐**: 取引時間内→③へ戻る／終了→⑨へ

### ⑨ 本日のレポート通知（詳細版）
- **担当**: `infrastructure/notification/line_notify_client.py`
- **内容: 詳細版（銘柄別損益まで含める）**
  - 当日の発注件数・約定状況（`OrderHistoryRepository`集計）
  - **銘柄別の評価損益額・評価損益率**（`GET /positions`の`ProfitLoss` / `ProfitLossRate`フィールドを使用。追加情報出力フラグを`true`にして取得する必要がある）
  - 当日合計の損益サマリ
- **異常系**: 通知失敗は取引処理に影響させないが、ログには残す

---

## 3. ループ制御の状態設計

| 状態 | 保持場所 | 更新タイミング |
|---|---|---|
| トークン | `KabuClient`内部 | ①(run_trading起動時)で取得、以降使い回し |
| 対象銘柄リスト（10件・固定） | `TradingUseCase`起動時状態 | 起動時に1回のみ`FilteringResultRepository`から取得。ループ中は不変 |
| 現金残高・保有株 | **都度取得（キャッシュしない）** | ⑤発注セーフティチェックのたびに`GET /wallet/cash`・`GET /positions`を呼び直す |
| 当日注文履歴 | `OrderHistoryRepository` | ⑥成功時に追記 |
| ループ継続フラグ | `TradingUseCase` | ⑧の判定結果で更新 |

---

## 4. 主要データモデル（domain/models.py）

| モデル | 主なフィールド | 用途 |
|---|---|---|
| `Board` | symbol, current_price, bid, ask, timestamp | ③の取得結果 |
| `CashBalance` | available_cash | ⑤で都度取得して使用 |
| `Position` | symbol, quantity, average_price, profit_loss, profit_loss_rate | ⑤⑨で使用 |
| `PriceLimit` | symbol, upper, lower | ④の判定パラメータ |
| `Signal` | symbol, signal_type(`SignalType`), reason | ④の出力・⑥⑨の監査ログ用 |
| `Order` | symbol, side(`OrderSide`), quantity, price_condition | ⑥の入力 |
| `OrderRecord` | order, result, executed_at, based_on(Signal) | 永続化・監査ログ・⑨のレポート集計用 |

---

## 6. キルスイッチ設計

3種類の閾値をすべて満たす必要がある（いずれか1つでも超えたら、以降の当日の新規発注を停止する）。

| 項目 | 考え方 | 判定方法 |
|---|---|---|
| 1回あたりの発注上限額 | **kabu側の`/apisoftlimit`（現物ワンショット上限）より厳しい固定額をアプリ側で別途設定**する。証券会社側の上限だけに依存せず、アプリ独自の安全マージンを持たせる | `GET /apisoftlimit`で取得した上限値と、`config/settings.py`のアプリ側固定額の**小さい方**を実効上限として⑤で比較 |
| 1日の最大発注回数 | **10回まで（対象銘柄数10件と同数）**。1銘柄あたり平均1回発注する想定を上限の目安とする | `OrderHistoryRepository`から当日の発注件数をカウントし、⑤で10件に達していないか確認 |
| 1日の想定損失上限 | **運用資金に対する一定割合**で設定する（固定額ではなく比率にすることで、運用資金が変わっても流用できる） | 当日の評価損益合計（`/positions`の`ProfitLoss`集計）が、運用資金×設定割合を下回った場合に新規発注を停止 |

- `config/settings.py`に以下の設定項目を追加する:
  - `MAX_ORDER_AMOUNT_PER_TRADE`（1回あたりの発注上限額）
  - `MAX_ORDER_COUNT_PER_DAY`（1日の最大発注回数、既定値10）
  - `DAILY_LOSS_LIMIT_RATIO`（1日の想定損失上限の運用資金に対する比率）
- `domain/rules.py`に`check_kill_switch(daily_orders, daily_pnl, capital, settings) -> bool`のような純粋関数として切り出す
- キルスイッチ発動時は⑨のレポートにその旨を明記し、通常のレポートと区別できるようにする

## 7. すり合わせ済み事項（2026-08-15）

- ①②③の連携: cronで時刻をずらすだけ（完了待ち合わせの仕組みは作らない。09:30②→09:35③）
- 現金残高・保有株のキャッシュ戦略: キャッシュせず、⑤発注のたびに毎回再取得
- 対象銘柄リスト（10件）: 1日を通して固定。場中の再フィルタは行わない
- ⑨レポート内容: 詳細版（`/positions`の評価損益額・評価損益率まで含める）
- キルスイッチ: 発注上限額(apisoftlimitより厳しい固定額)・最大発注回数(10回)・想定損失上限(運用資金の一定割合)の3条件

## 8. 残る未確定・要すり合わせ事項

1. **運用資金額そのもの、および想定損失上限の具体的な割合（何%か）**、1回あたりの発注上限額の具体的な金額 — これらは実際の運用資金が決まった段階で確定する数値パラメータ
2. ②の完了が9:35に間に合わなかった場合の具体的なリトライ・アラート方法（§1のガードはあるが、通知先・再実行手順は未設計）
3. ⑤の都度API取得によるレート制限（`/apisoftlimit`）への抵触有無の実測確認

## 9. 関連設計書

- `01-screening-design.md`（①前日スクリーニング、30〜50銘柄）
- `02-filtering-design.md`（②当日フィルタ、10銘柄。本設計書の入力元）
