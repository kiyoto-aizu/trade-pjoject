# trade-pjoject 詳細設計書 ⑥TradingUseCaseヒストリカル再生

対象: `TradingUseCase.run()`を過去の営業日・価格データで実行する疑似クライアントと、実行に必要な時刻制御。
関連ADR: [ADR-0007](../adr/0007-unify-trading-and-backtest.md)

## 1. 目的とスコープ

Phase 1では、Phase 2で`TradingUseCase.run()`を単一営業日再生するためのクライアント契約と既存実装の制約を定める。独自の注文約定・資金管理ロジックは実装しない。

- 注文約定・現金・保有株・平均取得価格は`PaperOrderClient`を再利用する。
- 新規に必要な責務は、過去の板価格、確定日足、日付・時刻の進行、および対象日のフィルタ結果選択である。
- 本番用の注文履歴、ペーパー口座状態、判定イベントDB、日次レポートを読み書きしない。
- 日足しかない日の場中価格を当日終値で代用しない。当日終値を場中に返すと先読みになるため、売買判定の再生には当該日の分足が必要。

## 2. 既存コードとの契約

`TradingUseCase`はProtocolではなく、必要なメソッドを実行時に参照するダックタイピングである。`order_sender`に`get_wallet_cash`、`get_positions`、`set_price`、`place_market_order`を持つ`PaperOrderClient`を渡せば、wallet/positionsクライアントを別途注入せずに口座状態を共有できる。

`PaperOrderClient(prices={}, cash=starting_cash, state_path=None, realized_pnl_date=target_date.isoformat())`は永続化を行わず、履歴日の実現損益基準日も明示できる。複数日再生では日ごとに新しい`TradingUseCase`と`PaperOrderClient`を作り、ポートフォリオ残高・保有株の持ち越し方法を別途定める。

`FilteringResultRepository`の実データディレクトリは`data/filtering/`であり、ファイル名は`YYYY-MM-DD.json`。`load_for_date()`があるため、履歴用リポジトリは対象日を選ぶ薄いアダプターとする。

## 3. 疑似クライアント設計

### 3.1 HistoricalClock

時計は現在時刻を返す`now()`と、次の分足時刻へ進める`advance()`を分離する。`now()`の呼び出しごとには時刻を進めない。

```python
class HistoricalClock:
    def __init__(self, trading_day: date, timestamps: list[datetime]): ...
    def now(self) -> datetime: ...
    def current_date(self) -> date: ...
    def advance(self, _seconds: float) -> None: ...
```

- `timestamps`は対象日の分足時刻を昇順に保持する。空配列は拒否する。
- 時計と既存`MinuteBar.time`はJSTのオフセットなし日時として扱い、比較前に同じ形式へ正規化する。
- `now()`は現在の要素を返すだけで、最後の要素到達後はその値を返し続ける。
- `advance()`は1回の呼び出しにつき1要素進め、引数の秒数は使用しない。`run()`の`sleep`引数には`clock.advance`を渡す。
- 最後の時刻は市場終了時刻以降とし、次のループ判定で`is_market_closed()`が終了させる。

この方式は`run()`が1周ごとに`sleep(config.LOOP_INTERVAL)`を呼ぶ構造に合わせる。`now_provider`は初期化時・ループ先頭に加えて注文回数判定でも呼ばれるため、`now()`自体を進める方式や即時リターンの`sleep`では分足を飛ばすかループが進まなくなる。

### 3.2 HistoricalBoardClient

```python
class HistoricalBoardClient:
    def __init__(self, price_series: dict[str, list[MinuteBar]], clock: HistoricalClock): ...
    def get_current_board(self, token: str, symbol: str) -> dict | None: ...
```

- `token`は使用しない。
- `MinuteBar.time`を日時へ変換し、`time <= clock.now()`を満たす最新バーの`price`を`{"current_price": price}`として返す。
- 対象時刻より後のバーは参照しない。対象時刻以前のバーが一つもなければ`None`を返す。
- 入力データは既存`ParquetMinuteBarRepository`から対象日・銘柄別に事前ロードできる。
- 日足終値・スリッページはこのクライアントから返さない。約定スリッページは`PaperOrderClient`に任せる。
- 分足がない営業日は場中価格を復元できないため、日足終値で場中判定するモードはPhase 1の売買再生対象に含めない。日足のみの対応は別設計が必要。

### 3.3 HistoricalMarketDataClient

```python
class HistoricalMarketDataClient:
    def __init__(self, daily_bars: dict[str, list[DatedDailyBar]], clock: HistoricalClock): ...
    def get_yahoo_daily_bars(self, symbol: str) -> list[DailyBar]: ...
    def get_yahoo_daily_closes(self, symbol: str) -> list[float]: ...
    def get_daily_ohlc(self, symbol: str, range_: str = "max") -> list[MarketDailyBar]: ...
```

- 株式・指数とも`bar.date < clock.current_date()`を満たす確定足だけを返す。当日足を含めない。
- `get_yahoo_daily_bars()`は日付を除いたdomain `DailyBar(high, low, close)`へ変換し、`get_yahoo_daily_closes()`は同じ順序で終値を返す。
- `MarketRegimeUseCase`は別の`get_daily_ohlc("^N225")`および`get_daily_ohlc("^VIX")`契約を要求する。履歴クライアントは指数データも受け持ち、同じ厳密な日付境界を適用する。
- レジーム判定を有効にする場合は、各シミュレーション日の日付で履歴データが切られたクライアントを使う。最新期間の判定を複数日に使い回さない。

### 3.4 HistoricalFilteringResultRepository

```python
class HistoricalFilteringResultRepository(FilteringResultRepository):
    def __init__(self, directory: Path, clock: HistoricalClock): ...
    def load_latest(self) -> FilteringResult | None: ...
```

`load_latest()`は`load_for_date(clock.current_date())`へ委譲する。`TradingUseCase.run()`は結果がない場合、または`result.date`が時計の日付と一致しない場合に取引を開始しないため、各再生日に対象日の結果ファイルが必要。

### 3.5 注文クライアント

```python
order_sender = PaperOrderClient(
    prices={},
    cash=starting_cash,
    state_path=None,
)
```

`TradingUseCase`が価格を取得した後、注文直前に`set_price()`を呼ぶため、同じインスタンスを`order_sender`に注入する。独自の`WalletClient`/`PositionsClient`/`OrderSender`は追加しない。複数日で資金・保有を持ち越す場合は、このインスタンスのポートフォリオ状態を維持しつつ、日次実現損益をシミュレーション日ごとに区切る。

## 4. Phase 2で必要なTradingUseCase側の隔離

疑似クライアントだけでは過去日を正しく再生できない。Phase 2の実行前に以下を対応するか、該当処理を明示的に無効化する。

| 現行の実日付・副作用 | 再生時の問題 | 対応状況 |
|---|---|---|
| `TradeSignal.to_order_history_entry()`と`is_duplicate_order()`/`is_recent_order()`が実時刻を既定参照 | 注文timestamp・日次重複判定・注文ロックが実行日依存になる | 解消。オプション時刻引数と`TradingUseCase._current_now`を使い、既定の本番挙動は実時計のまま |
| 買い注文回数の日付比較が時刻providerを追加で呼ぶ | 有限列providerや呼び出しごとに進む時計が枯渇・ずれる | 解消。現在ループ時刻`now`をそのまま使い、EODでも最後に取得した時刻を再利用 |
| `_send_end_of_day_report()`が実日時付で注文を抽出・レポート生成する | 過去日の注文がdaily reportから消える | 解消。日付、daily_orders、finalize時刻、`generated_at`に同じシミュレーション日時を使用。出力先は検証ごとのscratch |
| `PaperOrderClient`の日次実現損益リセットが`date.today()`を参照 | 指定した過去日の損益が最初の参照で消去される | 解消。`today_provider`を追加し、バックテストでは`clock.current_date`を注入 |
| `daily_analyzer=None`は分析無効ではなく既定LLM Analyzerの生成を意味する | EODごとに外部HTTP呼び出しが発生し得る | 検証CLIから明示的なNoOp Analyzerを注入 |
| `filter_decision_repository=None`はNoOpではなくSQLite Repositoryを生成する | 想定外のDB作成とティックごとのSQLite処理が発生する | 検証CLIからNoOpまたはrun専用SQLite Repositoryを注入 |
| `MarketRegimeUseCase.execute()`は実日付基準の指数Clientで判定し、結果をUseCase内に保持する | 複数日に同一UseCaseを使うと先読み・古い判定を再利用する | 単日では履歴指数Clientで確認済み。Phase 3は日ごとに新しい`TradingUseCase`を作成する |

検証CLIでは`get_api_soft_limit()`を設定値へ差し替え、`PaperOrderClient`、NoOp notifier、NoOp analyzerを使う。注文履歴、baseline、report、任意の判定DBはrunごとのscratch pathに分離する。

## 5. データ充足状況

2026-09-27にワークスペースを確認した結果、フィルタ結果は提案中の`data/filtering_results/`ではなく、実装が参照する`data/filtering/`に16ファイル存在する。期間は2026-09-01から2026-09-25で、うち2026-09-21は祝日なのに生成された異常ファイルである。稼働日判定に通るファイルは15日分で、3ヶ月分ではない。したがって、ADR-0007 Phase 3の短期通し検証にも日付ガードと分足の充足確認が要る。

対象期間を決める際は、単に開始日・終了日の範囲を取らず、各日のフィルタ結果と必要な全銘柄の分足が揃う日だけを実行対象にする。欠損日をスキップした場合は、結果に対象日・スキップ理由を記録する。

## 6. 性能検証の判断

`FilterDecisionRepository`は初期化時にSQLiteを用意し、観測更新メソッドは銘柄評価ループから呼ばれる。24.6万ティック規模で無視できるとは決めつけず、Phase 2で「実Repository」と「NoOp」を同一データ・同一銘柄数で計測する。正確性を優先して初回は専用DBを使い、計測後にNoOp化または書き込み頻度の変更を判断する。

## 7. Phase 1の完了条件とPhase 2着手条件

- [x] `PaperOrderClient`再利用と`state_path=None`を採用する。
- [x] 履歴データの厳密な先読み境界を`bar.date < simulated_date`とする。
- [x] フィルタ結果の実在範囲を確認する（2026-09-01〜2026-09-25、16ファイル。9/21は非稼働日）。
- [x] 時計は`now()`で進めず、注入`sleep`から1ティックずつ進める。
- [x] 時計・分足板価格・株式/指数日足・日付別フィルタRepositoryを実装し、契約テストを追加する。
- [x] PaperOrderClientの日次実現損益リセットをシミュレーション日基準にする。
- [x] 注文timestamp、重複判定、注文ロック、EOD抽出・生成日時を現在のシミュレーション時刻に揃える。
- [x] 9/25の指数OHLCと全対象銘柄の分足を実データで確認する。
- [x] 判定イベントRepositoryをNoOp/専用SQLiteで比較する。
- [x] 発注上限取得をローカル値へ差し替え、ペーパー注文とscratch出力だけで動くことを確認する。

Phase 1の疑似クライアント設計・実装・契約テストは完了した。日足しかない日の場中価格は先読みを避けるため意図的にサポートしない。

## 8. Phase 2単日検証結果 (2026-09-27)

- 2026-09-25は`is_trading_day`と`is_trading_session`の両方を通過し、当日フィルタ結果は10銘柄。全銘柄に分足があり合計900本、ユニーク時刻308点と15:30:01 sentinelを使って308ループを実行した。
- `MarketRegimeUseCase`は履歴指数データで`available`を返し、NORMAL/CAUTION/DANGERのうちCAUTIONと判定した。NoOp AnalyzerでLLM分析は実行されなかった。
- NoOpのみの実行は0.683秒。ペア計測ではNoOpが0.644秒、専用SQLite Repositoryが4.516秒で、SQLiteは3.872秒遅く約7.01倍。24.6万ティックへの単純外挿は約51.4秒と約360.7秒。実測は3080銘柄評価ティックに基づくため、外挿値は目安。
- NoOp/SQLiteの両実行で注文イベント数は2、SELLは持ち越し防止決済1件で一致した。`OrderHistoryEntry`に`note`属性はないため、検証CLIは`decision_reason`別に集計する。
- 既存`latest_timeseries.json`の9/25には銘柄3807の2往復（9:07買い→10:38売り、10:39買い→15:30売り）がある一方、新経路は同銘柄1往復だった。完全一致は求めない前提だが、この取引回数・売却時刻の差は未説明としてPhase 4の突合対象にする。
- 初回実行で注文履歴timestampとEOD日付が実行日になる機能バグを確認した。`PaperOrderClient.today_provider`、TradingUseCaseの保持中時計、注文重複判定への時刻伝搬で修正した後、注文2件のtimestampが9/25、EOD `date`/`generated_at`が9/25、`daily_orders`が2件であることを確認した。
- 板クライアントは分足なしで`None`を返す。`TradingUseCase.run()`は`not board`または`current_price is None`で当該銘柄をスキップする分岐を持つ。実対象日は分足欠損がなかったため、この連携ケースはコード分岐とクライアント単体テストで確認した。
- 時計をEODで再取得せず、`run()`の現在ティックを再利用することで、既存の有限回`now_provider`テストとの互換性も保った。フルテストは327件通過。

日足終値のみの場中再生と複数日ポートフォリオ状態の持ち越しは未対応。Phase 3では日ごとのUseCase再生成、状態持ち越し仕様、Phase 4差異の説明を先に設計する。