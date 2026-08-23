# config.py
import os
import sys
from enum import Enum
from pathlib import Path

try:
    from dotenv import load_dotenv
    _DOTENV_AVAILABLE = True
except ImportError:
    _DOTENV_AVAILABLE = False

_repo_root = Path(__file__).resolve().parents[1]
_env_path = _repo_root / '.env'
if _DOTENV_AVAILABLE and _env_path.exists():
    load_dotenv(dotenv_path=str(_env_path))


def _is_test_runtime() -> bool:
    return "pytest" in sys.modules or os.getenv("ALLOW_MISSING_ENV", "").strip().lower() in {"1", "true", "yes", "on"}


class OrderSide(str, Enum):
    SELL = "1"
    BUY = "2"


def _load_required_env(name: str, *, allow_missing: bool | None = None) -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value

    if allow_missing is None:
        allow_missing = _is_test_runtime()
    if allow_missing:
        return ""

    raise ValueError(f"Missing required environment variable: {name}")


_IS_DEMO_ENV = os.getenv("IS_DEMO", "true").strip().lower()
IS_DEMO = _IS_DEMO_ENV in ("1", "true", "yes")
ENABLE_LIVE_ORDERING = os.getenv("ENABLE_LIVE_ORDERING", "false").strip().lower() in ("1", "true", "yes")
_ALLOW_MISSING_ENV = _is_test_runtime()

# 💡 銘柄ごとに個別の売買基準値を設定（辞書形式）
# 「コード」: {"buy": 買い基準値, "sell": 売り基準値}
PRICE_LIMITS = {
    "1475": {"buy": 2735.0, "sell": 2760.0}  # ダミー
#    "7203": {"buy": 2735.0, "sell": 2760.0},  # トヨタ
#    "8306": {"buy": 1490.0, "sell": 1515.0},  # 三菱UFJ
#    "6758": {"buy": 13950.0, "sell": 14050.0}  # ソニー
}

TARGET_SYMBOLS = list(PRICE_LIMITS.keys())
LOOP_INTERVAL = 60
DEFAULT_ORDER_QTY = 100
ORDER_LOCK_SECONDS = 60
MAX_ORDER_AMOUNT_PER_TRADE = float(os.getenv("MAX_ORDER_AMOUNT_PER_TRADE", "100000"))
MAX_ORDER_COUNT_PER_DAY = int(os.getenv("MAX_ORDER_COUNT_PER_DAY", "10"))
DAILY_LOSS_LIMIT_RATIO = float(os.getenv("DAILY_LOSS_LIMIT_RATIO", "0.02"))
API_SOFT_LIMIT = float(os.getenv("API_SOFT_LIMIT", "1000000"))
OPERATING_CAPITAL = float(os.getenv("OPERATING_CAPITAL", "1000000"))
ORDER_HISTORY_FILE = "order_history.json"
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30
LINE_MESSAGE_CHANNEL_TOKEN = os.getenv("LINE_MESSAGE_CHANNEL_TOKEN", "")
LINE_MESSAGE_TO = os.getenv("LINE_MESSAGE_TO", "")
LINE_MESSAGE_API = "https://api.line.me/v2/bot/message/push"

API_PORT = os.getenv("API_PORT_DEV", "18081") if IS_DEMO else os.getenv("API_PORT_PRD", "18080")
API_PASSWORD = _load_required_env("API_PASSWORD_DEV" if IS_DEMO else "API_PASSWORD_PRD", allow_missing=_ALLOW_MISSING_ENV)

if not IS_DEMO and not ENABLE_LIVE_ORDERING:
    raise ValueError(
        "Production mode requires ENABLE_LIVE_ORDERING=true. "
        "Set IS_DEMO=false and ENABLE_LIVE_ORDERING=true to allow live orders."
    )

BASE_URL = f"http://localhost:{API_PORT}/kabusapi"

LOG_FILE_PATH = str(_repo_root / "trade_project.log")
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", str(5)))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()
