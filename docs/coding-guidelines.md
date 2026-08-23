# trade-pjoject コーディング規約・設計方針 第2版

このドキュメントは、trade-pjoject でコードを書く／レビューする際に従うべきルールをまとめたものです。
AIにコードを書かせる・レビューさせる際は、このファイルをコンテキストとして読み込ませてください。
（現状コードの個別の問題点は [review-current-code.md](./review-current-code.md) 側にまとめています）

---

## 1. レイヤー構成

```
trade-pjoject/
├── config/
│   └── settings.py                 # 設定の定義・検証のみ。副作用なし
├── domain/
│   ├── enums.py                    # OrderSide, SignalType など
│   ├── models.py                   # Domainモデル（Order, Position, PriceLimit など）
│   └── rules.py                    # 判断ロジック・安全チェック
├── application/
│   ├── trading_usecase.py          # 売買実行ユースケース
│   ├── screening_usecase.py        # 銘柄スクリーニングユースケース
│   └── filtering_usecase.py        # 動的評価ユースケース
├── infrastructure/
│   ├── kabu/
│   │   ├── kabu_client.py
│   │   ├── board_repository.py
│   │   ├── wallet_repository.py
│   │   ├── position_repository.py
│   │   └── order_repository.py
│   ├── market_data/
│   │   └── yahoo_finance_client.py
│   ├── notification/
│   │   └── line_notify_client.py
│   └── persistence/
│       └── order_history_repository.py
├── entrypoints/
│   ├── run_trading.py              # 起動トリガー別の薄いラッパー
│   ├── run_screening.py
│   └── run_filtering.py
├── main.py                         # デフォルト実行・CLI用のランナー
└── tests/
    ├── domain/
    ├── application/
    └── infrastructure/
```

### 1.1 各層の責務と依存方向
- **依存の向きは一方向**：`main → entrypoints → application → domain` / `application → infrastructure`。
- `domain` は外部依存を持たず、ビジネスルールと判定ロジックだけを表現する。
- `application` はユースケースの司令塔で、`domain` のロジックと `infrastructure` の入出力をつなぐ。
- `infrastructure` は外部API・DB・通知・永続化の具体実装のみを担当し、ロジックは持たない。
- `entrypoints` は起動時の「初期化」と「ユースケース呼び出し」だけを担当する。

```python
# domain/enums.py
from enum import Enum

class OrderSide(str, Enum):
    BUY = "2"
    SELL = "1"

# application/evaluate_symbol_usecase.py
class EvaluateSymbolUseCase:
    def __init__(self, board_repo, order_repo, safety_checker):
        self._board_repo = board_repo
        self._order_repo = order_repo
        self._safety_checker = safety_checker

    def execute(self, symbol: str, limits: PriceLimit) -> Optional[Signal]:
        board = self._board_repo.get_current_board(symbol)
        if board is None or board.current_price is None:
            # データが取れない場合は評価をスキップする。価格を推測・捏造しない。
            return None
        ...
```

---

## 2. 命名規約

### 2.1 ファイル・モジュール
- ファイル名と `import` 時のモジュール名は必ず一致させる。
- 責務が「取得」なら `get_xxx.py`、「送信・実行」なら `send_xxx.py` / `place_xxx.py`、「判定」なら `decide_xxx.py` のように動詞プレフィックスで統一する。
- 中身から役割が読み取れない汎用的な名前（`component`, `executor`, `filter_dynamic` など）は避け、扱う対象・レイヤーが分かる名前にする。
- `entrypoints/` 以下は「起動トリガー」を表す名前にする。例: `run_trading.py`, `run_screening.py`。

### 2.2 クラス
- 1クラス1責務。役割を表す接尾辞で統一する。
  - `Repository`: 永続化・外部データ取得
  - `Client`: 外部API通信
  - `UseCase`: 業務フロー
  - `Service`: 横断的な処理
- 実態と乖離した名前（例: 移動平均計算なのに「Ai」と付ける）は避け、処理内容に即した名前にする。

### 2.3 変数・定数
- 意味のある値（発注方向など）は文字列直書きせず `Enum` にする。
- 定数は `UPPER_SNAKE_CASE`、意味単位でグループ化する。
- 真偽値フラグは `is_` / `has_` プレフィックスで統一する。

### 2.4 関数
- 「取得して判定して発注する」のような複合動詞を避け、1関数1動作にする。
- 副作用（ファイル書き込み・API呼び出し・print）を持つ関数と、純粋な計算関数を名前で区別できるようにする。
  - 計算のみ: `calculate_xxx`
  - 外部影響あり: `execute_xxx`, `send_xxx`, `persist_xxx`, `notify_xxx`

---

## 3. セキュリティ観点

### 3.1 秘密情報管理
- `.env` は必ず `.gitignore` に含める。公開リポジトリの場合はコミット履歴に過去のシークレットが残っていないかも確認する。
- 必須環境変数（APIパスワード、通知トークン等）は、値が空でもデフォルト値で起動を許容せず、欠けている場合は起動時に例外で停止する。
- ログ・例外メッセージにトークンやパスワードを含めない。

### 3.2 本番/デモ切り替えの安全化
- 環境切り替えは1つのフラグだけで実弾発注に到達しない設計にする。
- 起動引数や環境変数の二重チェックを要求する。
- 発注数量の上限・1日の最大発注回数・想定損失額の上限（キルスイッチ）を設定し、超えたら自動停止する。

### 3.3 外部API呼び出しの安全性
- 外部データ取得に失敗した場合、フォールバックとして推測・乱数生成した値を使わない。
- 非公式APIやレート制限のあるAPIを使う場合は、タイムアウト・リトライ間隔・サーキットブレーカーを設ける。
- 注文履歴データが破損している場合は、空初期化でサイレントに継続せず、明示的な警告または起動停止で気づける形にする。

### 3.4 監査ログ
- 「いつ・どの銘柄を・いくらで・何を根拠に」発注したかを、判定に使った数値とともにログに残す。
- 標準出力だけに頼らず、ログファイル／監査ログに記録する。

---

## 4. テスト観点

### 4.1 基本方針
- `domain` 層（シグナル判定・安全性チェックのロジック）は外部APIから完全に切り離し、ネットワークなしでテストできる状態を保つ。
- `infrastructure` 層は repository/client のインターフェースを介して呼び出せるようにし、テスト時にモックへ差し替え可能にする。

### 4.2 テストすべき観点

**シグナル判定ロジック**
- 境界値テスト（閾値ちょうど、わずかに上、わずかに下）
- 入力データが不足しているときにスキップされること
- 閾値計算（移動平均など）が正しいこと

**安全性チェック**
- 予算不足時に発注が拒否されること
- 同一銘柄・同一方向の当日注文がある場合に拒否されること
- 二重発注防止のロック時間の境界値
- 保有がない銘柄への売り注文が拒否されること

**永続化**
- 初回起動（ファイル不存在）時の挙動
- データ破損時の挙動（サイレントに空初期化しない）
- 複数回の記録が正しく積み上がること

**異常系**
- 外部APIがエラー・タイムアウトを返したときに、後続の注文ロジックへ進まないこと
- 複数の外部APIのうち一部が落ちても、他の処理に影響しないこと

### 4.3 テストの種類
- **ユニットテスト**: `domain` / `application` 層はモックのみで完結させる。
- **契約テスト**: `infrastructure` 層は外部APIのレスポンス形式に依存するため、サンプルレスポンスを使ったパース処理のテストを用意する。
- **結合テスト**: デモ環境を使って、起動〜1サイクル評価〜レポート送信までを通しで動かす。

---

## 5. 起動契機とエントリポイント（第2版）

- 起動契機が異なる場合、エントリポイントを分けるのは許容される。
- ただし、エントリポイントは「薄いラッパー」にとどめる。
- 実ビジネスロジックは `application` / `domain` に置き、`entrypoints/` は依存関係の組み立てとユースケース呼び出しだけを行う。
- `main.py` は `default` または `CLI` 用のランナーとして残してもよい。

### 5.1 例: 良い構成

```
src/
  entrypoints/
    run_trading.py
    run_screening.py
    run_filtering.py
  main.py
  application/
  domain/
  infrastructure/
  config/
```

### 5.2 `entrypoints` の責務
- 設定読み込み
- ロギング初期化
- 依存オブジェクトの生成
- ユースケースの呼び出し
- `if __name__ == '__main__'` のみを持つ

### 5.3 `entrypoints` を分けるべきケース
- スケジュール実行と手動実行で起動条件が異なる場合
- サービス起動とバッチ実行で初期化プロセスが異なる場合
- Webhook など、外部トリガーが専用起動フローを必要とする場合

### 5.4 エントリポイントのサンプル

```python
# entrypoints/run_trading.py
from config.settings import settings
from infrastructure.kabu.kabu_client import KabuClient
from infrastructure.market_data.yahoo_finance_client import YahooFinanceClient
from infrastructure.notification.line_notify_client import LineNotifyClient
from infrastructure.persistence.order_history_repository import OrderHistoryRepository
from application.trading_usecase import TradingUseCase
from infrastructure.logging import configure_logging


def main():
    configure_logging(settings)
    kabu = KabuClient(settings)
    market_data = YahooFinanceClient()
    notifier = LineNotifyClient(settings)
    history = OrderHistoryRepository(settings.order_history_path)

    usecase = TradingUseCase(
        kabu_client=kabu,
        market_data_client=market_data,
        notifier=notifier,
        order_history_repository=history,
    )
    usecase.run()


if __name__ == '__main__':
    main()
```

---

## 6. 移行の進め方

1. 危険なフォールバック処理（推測値での代替）を除去する
2. 設定読み込み時の副作用（print等）を除去し、必須環境変数のfail-fast化を行う
3. マジックストリングを `Enum` 化する
4. 既存の外部連携コードを `infrastructure` 配下に整理する
5. 判定・安全性チェックのロジックを `domain` 層に純粋関数として抽出する
6. `domain` / `application` にユニットテストを追加する
7. 司令塔クラスを `application` 層のユースケースに分割する
