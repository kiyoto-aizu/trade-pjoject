# trade-project

三菱UFJ eスマート証券の **kabuステーションAPI** と Yahoo Finance を利用した、株式の自動スクリーニング・フィルタリング・取引システムです。

## 概要

本システムは、次の3段階で取引対象を決定します。

1. **スクリーニング**: kabuステーションのランキングから候補銘柄を抽出
2. **フィルタリング**: 出来高急騰率を計算し、候補を上位銘柄に絞り込み
3. **取引実行**: 移動平均を基準に売買シグナルを生成し、注文を送信

ビジネスロジックを `domain` 層に集約し、API・永続化・通知などの外部依存を分離しています。

## 主な機能

### スクリーニング

- kabuステーションAPI の `GET /ranking` から売買代金・値上がり率ランキングを取得
- 複数ランキングを順位合算方式で統合
- 規制銘柄と対象外市場の銘柄を除外
- 前日大引け後から翌朝のランキングデータクリア前に実行

### フィルタリング

- 前日のスクリーニング結果を読み込み
- kabuステーションから当日の出来高を取得
- Yahoo Finance から過去20営業日の平均出来高を取得
- 出来高急騰率の上位銘柄を選択

### 取引

- 過去5日間の終値から移動平均を計算
- `MA * 0.99` 以下で買い、`MA * 1.01` 以上で売りシグナルを生成
- 資金、保有株、重複注文、注文間隔、注文数などを確認
- 取引終了後にLINEで結果を通知

## アーキテクチャ

```text
entrypoints       起動処理と依存性の組み立て
       |
application       ユースケースの実行制御
       |
domain            モデルと外部依存のないビジネスルール
       |
infrastructure    kabuステーション、Yahoo Finance、JSON、LINE
```

| 層 | 主な責務 |
| --- | --- |
| `domain` | ドメインモデル、列挙型、売買ルール |
| `application` | スクリーニング・フィルタリング・取引のユースケース |
| `infrastructure` | 外部API、マーケットデータ、永続化、通知 |
| `entrypoints` | 各処理の起動と依存性注入 |

## プロジェクト構成

```text
src/
├── application/       ユースケース
├── config/            環境設定
├── domain/            モデル、Enum、ビジネスルール
├── entrypoints/       スクリーニング等の起動処理
├── infrastructure/
│   ├── api/            HTTP共通処理
│   ├── kabu/           kabuステーションAPI連携
│   ├── market_data/    Yahoo Finance連携
│   ├── notification/   LINE通知
│   └── persistence/    JSON永続化
├── screening/          スクリーニング実行ラッパー
├── filter_dynamic/     フィルタリング実行ラッパー
├── trading/            TradingBot
└── executor/           取引実行ラッパー

docs/                   設計書、フロー図、コーディング規約
references/             kabuステーションAPI仕様書
tests/                  pytestテスト
```

## セットアップ

### 前提条件

- Python 3.10以上
- 三菱UFJ eスマート証券の口座
- kabuステーションのインストールと起動
- Yahoo Financeへ接続できる環境
- LINE通知を利用する場合はMessaging APIの設定

### インストール

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 環境変数

`.env` またはシステム環境変数に設定してください。パスワードなどの秘密情報はコミットしないでください。

```env
IS_DEMO=true
API_PASSWORD_DEV=<デモ環境のAPIパスワード>
API_PASSWORD_PRD=<本番環境のAPIパスワード>
API_PORT_DEV=18081
API_PORT_PRD=18080
ENABLE_LIVE_ORDERING=false

LINE_MESSAGE_CHANNEL_TOKEN=<チャネルアクセストークン>
LINE_MESSAGE_TO=<送信先ユーザーID>

MAX_ORDER_AMOUNT_PER_TRADE=100000
MAX_ORDER_COUNT_PER_DAY=10
DAILY_LOSS_LIMIT_RATIO=0.02
OPERATING_CAPITAL=1000000
API_SOFT_LIMIT=1000000
```

`IS_DEMO=false` の場合でも、実注文を有効にするには `ENABLE_LIVE_ORDERING=true` を明示的に設定する必要があります。

## 実行方法

### 1. スクリーニング

前日15:35頃に実行し、翌営業日の候補銘柄を保存します。

```powershell
python -m src.entrypoints.run_screening
```

### 2. フィルタリング

営業開始後の9:00頃に実行し、出来高急騰率の高い銘柄へ絞り込みます。

```powershell
python -m src.entrypoints.run_filtering
```

### 3. 取引

営業開始前に起動し、15:30まで価格を監視します。

```powershell
python -m src.entrypoints.run_trading
```

## 取引フロー

```text
前日15:35頃
  スクリーニング -> 候補銘柄を保存
       |
当日9:00頃
  フィルタリング -> 出来高急騰銘柄を保存
       |
当日9:00-15:30
  価格監視 -> シグナル判定 -> 安全性確認 -> 注文
       |
15:30
  取引終了 -> 最終状態を通知
```

## 設計書

- [コーディング規約](docs/coding-guidelines.md)
- [全体フロー](docs/flow.md)
- [スクリーニング詳細設計](docs/detail_design/01-screening-design.md)
- [フィルタリング詳細設計](docs/detail_design/02-filtering-design.md)
- [取引ループ詳細設計](docs/detail_design/03-trading-loop-design.md)
- [kabuステーションAPI仕様書](references/kabu_STATION_API.yaml)

## テスト

```powershell
pytest -q
```

個別に実行する場合：

```powershell
pytest tests/test_trading_bot.py -v
pytest tests/test_design_alignment.py -v
```

## 注意事項

- kabuステーションが起動していない場合、API通信は失敗します。
- 本番注文を有効化する前に、必ずデモ環境で動作を確認してください。
- 投資判断と注文結果は利用者自身の責任で管理してください。
