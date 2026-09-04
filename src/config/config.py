# config.py
# ================================================================================
# 取引ボットの設定モジュール
# APIクレデンシャル、取引パラメータ、環境固有の設定など、
# すべての設定を管理するモジュール
# ================================================================================


import os
import sys
from pathlib import Path

from src.domain.enums import OrderSide  # noqa: F401  # config.OrderSide として再エクスポート（重複定義を避ける）

# .envファイルから環境変数を読み込む（利用可能な場合）
try:
    from dotenv import load_dotenv
    _DOTENV_AVAILABLE = True
except ImportError:
    _DOTENV_AVAILABLE = False

# リポジトリのルートディレクトリを取得
# config.py -> config/ -> src/ -> リポジトリルート の順にたどる。
_repo_root = Path(__file__).resolve().parents[2]
_env_path = _repo_root / '.env'
if _DOTENV_AVAILABLE and _env_path.exists():
    load_dotenv(dotenv_path=str(_env_path))


# ================================================================================
# ランタイム検出関数
# ================================================================================

def _is_test_runtime() -> bool:
    """
    テストモードで実行されているかを判定する。
    pytestがアクティブまたはALLOW_MISSING_ENVが明示的に設定されている場合、Trueを返す。
    """
    return "pytest" in sys.modules or os.getenv("ALLOW_MISSING_ENV", "").strip().lower() in {"1", "true", "yes", "on"}


# ================================================================================
# 環境変数読み込み関数
# ================================================================================

def _load_required_env(name: str, *, allow_missing: bool | None = None) -> str:
    """
    必須環境変数を読み込む。
    
    Args:
        name: 読み込む環境変数名
        allow_missing: Trueの場合、見つからないときは空文字列を返す。Noneの場合はテストランタイムの動作を使用。
    
    Returns:
        環境変数の値、またはallow_missingがTrueの場合は空文字列
    
    Raises:
        ValueError: 変数が見つからずallow_missingがFalseの場合
    """
    value = os.getenv(name, "").strip()
    if value:
        return value

    if allow_missing is None:
        allow_missing = _is_test_runtime()
    if allow_missing:
        return ""

    raise ValueError(f"Missing required environment variable: {name}")

# ================================================================================
# 環境モード設定
# ================================================================================

_IS_DEMO_ENV = os.getenv("IS_DEMO", "true").strip().lower()
IS_DEMO = _IS_DEMO_ENV in ("1", "true", "yes")  # True: デモモード、False: 本番モード
ENABLE_LIVE_ORDERING = os.getenv("ENABLE_LIVE_ORDERING", "false").strip().lower() in ("1", "true", "yes")  # 実取引を許可
TRADING_MODE = os.getenv("TRADING_MODE", "paper").strip().lower()
if TRADING_MODE not in {"paper", "live"}:
    raise ValueError("TRADING_MODEはpaperまたはliveを指定してください。")
_ALLOW_MISSING_ENV = _is_test_runtime()  # テスト中は環境変数がなくても許可


# ================================================================================
# 取引価格設定
# ================================================================================

# 対象株式ごとの個別価格閾値
# フォーマット: "銘柄コード": {"buy": 買い閾値, "sell": 売り閾値}
# これらの閾値は取引機会を識別するために使用される
PRICE_LIMITS = {
    "1475": {"buy": 2735.0, "sell": 2760.0}  # ダミー例
#    "7203": {"buy": 2735.0, "sell": 2760.0},  # トヨタ
#    "8306": {"buy": 1490.0, "sell": 1515.0},  # 三菱UFJ
#    "6758": {"buy": 13950.0, "sell": 14050.0}  # ソニー
}

# ================================================================================
# 取引パラメータ
# ================================================================================

# 取引対象の銘柄リスト（PRICE_LIMITSのキーから抽出）
TARGET_SYMBOLS = list(PRICE_LIMITS.keys())

# メイン取引ループの実行間隔（秒）
LOOP_INTERVAL = 60

# 1回の注文あたりのデフォルト数量
DEFAULT_ORDER_QTY = 100

# 同じ銘柄の重複注文を防ぐためのロック期間（秒）
ORDER_LOCK_SECONDS = 60

# 1回の取引あたりの最大注文金額
MAX_ORDER_AMOUNT_PER_TRADE = float(os.getenv("MAX_ORDER_AMOUNT_PER_TRADE", "100000"))

# 1日あたりの最大注文数
MAX_ORDER_COUNT_PER_DAY = int(os.getenv("MAX_ORDER_COUNT_PER_DAY", "10"))

# 1日の損失限度額（運用資本の比率、例：0.02 = 2%）
DAILY_LOSS_LIMIT_RATIO = float(os.getenv("DAILY_LOSS_LIMIT_RATIO", "0.02"))

# APIソフトリミット - リスク管理用の内部閾値
API_SOFT_LIMIT = float(os.getenv("API_SOFT_LIMIT", "1000000"))

# kabuステーションAPIの実行回数制限を超えないための最小呼出間隔（秒）
API_REQUEST_INTERVAL_SECONDS = float(os.getenv("API_REQUEST_INTERVAL_SECONDS", "0.12"))

# 取引に利用可能な運用資本
OPERATING_CAPITAL = float(os.getenv("OPERATING_CAPITAL", "1000000"))

# スクリーニング対象とする1株あたりの株価上限
MAX_SHARE_PRICE = float(os.getenv("MAX_SHARE_PRICE", "300"))

# ================================================================================
# 注文履歴・市場設定
# ================================================================================

# 注文履歴を保存するファイル
ORDER_HISTORY_FILE = "order_history.json"
PAPER_ACCOUNT_STATE_FILE = "paper_account_state.json"

# 市場クローズ時刻（日本標準時）
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 0
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30


# ================================================================================
# LINE通知設定
# ================================================================================

# LINE Messaging APIの認証情報（アラート送信用）
LINE_MESSAGE_CHANNEL_TOKEN = os.getenv("LINE_MESSAGE_CHANNEL_TOKEN", "")
LINE_MESSAGE_TO = os.getenv("LINE_MESSAGE_TO", "")
LINE_MESSAGE_API = "https://api.line.me/v2/bot/message/push"


# ================================================================================
# API設定
# ================================================================================

# 環境に応じたAPIポート選択（デモ/本番）
API_PORT = os.getenv("API_PORT_DEV", "18081") if IS_DEMO else os.getenv("API_PORT_PRD", "18080")

# 環境に応じたAPIパスワード選択
API_PASSWORD = _load_required_env("API_PASSWORD_DEV" if IS_DEMO else "API_PASSWORD_PRD", allow_missing=_ALLOW_MISSING_ENV)

# バリデーション: 本番モードは実取引が明示的に有効化されている必要がある
if not IS_DEMO and not ENABLE_LIVE_ORDERING:
    raise ValueError(
        "本番モードでは ENABLE_LIVE_ORDERING=true が必須です。 "
        "実取引を許可するには IS_DEMO=false と ENABLE_LIVE_ORDERING=true を設定してください。"
    )

# Kabu.com Station APIのベースURL
BASE_URL = f"http://localhost:{API_PORT}/kabusapi"


# ================================================================================
# ログ設定
# ================================================================================

# アプリケーションログのファイルパス
LOG_FILE_PATH = str(_repo_root / "trade_project.log")

# ログファイルのローテーション前の最大サイズ（バイト）
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))

# 保持するバックアップログファイルの数
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", str(5)))

# ログレベル (DEBUG, INFO, WARNING, ERROR, CRITICAL)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
