# trade-pjoject

クリーンな `domain` / `application` / `infrastructure` レイヤー構成を採用した Python 製トレーディング自動化プロジェクトです。

## 概要

`trade-pjoject` は、Kabu API 連携、マーケットデータ取得、通知送信を通じて売買フローを実行するためのフレームワークです。
ビジネスロジックをインフラ依存から切り離し、複数の起動パスを許容しながらもコアロジックを中央に集約する設計を目指しています。

## プロジェクト構成

- `src/config/`: 環境設定の読み込みと検証
- `src/domain/`: ドメインモデル、列挙型、ビジネスルール
- `src/application/`: ユースケースとオーケストレーション
- `src/infrastructure/`: 外部 API クライアント、永続化、通知
- `src/entrypoints/`: 起動時の薄いラッパー
- `docs/`: コーディング規約と設計方針
- `tests/`: ユニット/結合テスト

## セットアップ

1. 依存関係をインストールします。

```powershell
pip install -r requirements.txt
```

2. `.env` ファイルまたはシステム環境変数で設定を準備します。
   - シークレットはバージョン管理に含めないでください。

3. デフォルトのエントリポイントを実行します。

```powershell
python src/main.py
```

4. 特定のワークフローを実行する場合は、エントリポイントを指定します。

```powershell
python src/entrypoints/run_trading.py
python src/entrypoints/run_screening.py
python src/entrypoints/run_filtering.py
python src/entrypoints/run_executor.py
```

## アーキテクチャ

このリポジトリはレイヤードアーキテクチャに従います。

- `domain`: 外部依存なしの純粋なビジネスロジック
- `application`: ユースケースとビジネスオーケストレーション
- `infrastructure`: API クライアント、ストレージ、通知などの具体実装
- `entrypoints`: 起動時の依存注入とユースケース呼び出しのみ

詳細は `docs/coding-guidelines.md` を参照してください。

## テスト

`pytest` でテストを実行します。

```powershell
pytest -q
```

## 注意事項

- `entrypoints` は薄いラッパーに留め、ビジネスロジックは `application` / `domain` に置きます。
- 本番コードでは `print()` を使わず、`logging` を使います。
- 注文方向や環境モードなど固定値は `Enum` で管理します。
