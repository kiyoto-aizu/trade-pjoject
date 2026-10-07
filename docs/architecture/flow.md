# 全体のデータの流れ

各機能が受け渡す永続データと、その保存場所を示します。保存先はコードと `data/用途_*.md` で確認できたものに限っています。

```mermaid
flowchart LR
    JPX[JPX上場銘柄一覧] --> Master[上場銘柄マスタ]
    Master --> Screening[スクリーニング]
    Screening --> ScreeningResult[日別スクリーニング結果]
    ScreeningResult --> Filtering[フィルタリング]
    Filtering --> FilteringResult[日別フィルタリング結果]
    FilteringResult --> Trading[取引]
    Trading --> TradeState[注文履歴・口座状態]
    Trading --> DecisionDB[判定イベントDB]
    Trading --> DailyReport[日次運用レポート]
    FilteringResult --> Backfill[分足バックフィル]
    Backfill --> MinuteParquet[分足Parquet]
    MinuteParquet --> Backtest[バックテスト]
    FilteringResult --> Backtest
    Backtest --> BacktestResults[バックテスト結果]
    DailyReport --> DailyAnalysis[日次分析・答え合わせ]
    FilteringResult --> DailyAnalysis
    DailyCache[日足キャッシュ] --> DailyAnalysis
    DailyAnalysis --> TrendDB[トレンド答え合わせDB]
    DailyReport --> PeriodAnalysis[週次・月次分析]
    BacktestResults --> PeriodAnalysis
    DecisionDB --> PeriodAnalysis
    TrendDB --> PeriodAnalysis
    PeriodAnalysis --> PeriodReports[週次・月次レポート]
    DailyReport --> Diary[日記]
    BacktestResults --> Diary
    Notes[手動運用メモ] --> Diary
    Diary --> DiaryOutput[日記出力]
    PeriodReports --> StrategyReview[戦略レビュー]
    DailyCache --> StrategyReview
    MinuteParquet --> StrategyReview
    DecisionDB --> StrategyReview
    StrategyReview --> Hypotheses[仮説・検証結果]
    LLM[任意のLLM連携]
    DailyAnalysis -.分析文.-> LLM
    PeriodAnalysis -.分析文.-> LLM
    Backtest -.分析文.-> LLM
    Diary -.日記生成.-> LLM
    StrategyReview -.仮説生成・検証.-> LLM
```

表は永続化される主な受け渡しデータです。`日足キャッシュ`の内部ファイル形式はこの資料では特定せず、確認対象として残します。

## データの置き場所

| データ | 作る機能 | 読む機能 | 保存場所 | 形式 |
|---|---|---|---|---|
| 上場銘柄マスタ | 上場銘柄マスタ更新 | スクリーニング、過去検証 | `data/universe/listed_securities.csv` | CSV |
| スクリーニング結果 | スクリーニング | フィルタリング、分足バックフィル、バックテスト | `data/screening/{日付}.json` | JSON |
| フィルタリング結果 | フィルタリング | 取引、分足バックフィル、バックテスト、分析 | `data/filtering/{日付}.json` | JSON |
| 注文履歴・口座状態 | 取引 | 取引の再起動後復元、分析 | `data/trading/` | JSON、非常停止フラグ |
| 判定イベント | 取引、バックテスト | 取引・週次/月次分析・戦略レビュー | `data/state/filter_decision_events.sqlite3`、`data/backtest/filter_events/` | SQLite |
| 日足キャッシュ | 日次分析内の日足更新 | 日次分析・答え合わせ、戦略レビュー | `data/cache/yahoo_daily/` | 要確認 |
| 分足データ | 分足バックフィル | 分足バックテスト、戦略レビュー | `data/minute_bars_parquet/symbol=.../date=.../data.parquet` | Parquet |
| 運用レポート | 取引、日次・週次・月次分析 | 日次・週次・月次分析、日記、戦略レビュー | `data/reports/daily/`、`weekly/`、`monthly/` | JSON |
| トレンド答え合わせ結果 | 日次分析・答え合わせ | 日次・週次・月次分析 | `data/analysis/trend_check.sqlite3`、`data/reports/trend_check/` | SQLite、JSON |
| バックテスト結果 | バックテスト | 週次・月次分析、日記 | `data/backtest/latest/`、`runs/` | JSON |
| 日記 | 日記 | 手動参照 | `data/notes/diary/` | JSON、Markdown |
| 戦略レビューの仮説・検証結果 | 戦略レビュー | 手動参照 | `data/strategy_hypotheses/`、`data/strategy_verification/` | 要確認 |

図に示した各機能のレイヤー構成は[コーディング規約](coding-guidelines.md)を参照してください。実装上の保存先と読書き関係を確認できないデータの流れは、推測で補わず「要確認」としています。
