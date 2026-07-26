# config.py
import os
from pathlib import Path

# Attempt to load environment variables from a repository-level .env file when available.
# This is optional and falls back silently if python-dotenv isn't installed.
try:
    from dotenv import load_dotenv
    _DOTENV_AVAILABLE = True
except Exception:
    _DOTENV_AVAILABLE = False

# Look for .env at the repository root (one level above src/)
_repo_root = Path(__file__).resolve().parents[1]
_env_path = _repo_root / '.env'
if _DOTENV_AVAILABLE and _env_path.exists():
    load_dotenv(dotenv_path=str(_env_path))

# ────────────────────────────────────────────────────────
# 【共通設定】
# ────────────────────────────────────────────────────────
# ────────────────────────────────────────────────────────
# 【共通設定】
# ────────────────────────────────────────────────────────
# 💡 銘柄ごとに個別の売買基準値を設定（辞書形式）
# 「コード」: {"buy": 買い基準値, "sell": 売り基準値}
PRICE_LIMITS = {
    "1475": {"buy": 2735.0, "sell": 2760.0}  # ダミー
#    "7203": {"buy": 2735.0, "sell": 2760.0},  # トヨタ
#    "8306": {"buy": 1490.0, "sell": 1515.0},  # 三菱UFJ
#    "6758": {"buy": 13950.0, "sell": 14050.0}  # ソニー
}

# 監視対象の銘柄リストは、上記の登録データから自動的に抽出します
TARGET_SYMBOLS = list(PRICE_LIMITS.keys())

# ループの待機時間（60秒固定）
LOOP_INTERVAL = 60

# デフォルト発注数量
DEFAULT_ORDER_QTY = 100

# 安全確認用設定
ORDER_LOCK_SECONDS = 60
ORDER_HISTORY_FILE = "order_history.json"

# 取引終了時刻（24時間表記）
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30

# LINE Message API 設定
LINE_MESSAGE_CHANNEL_TOKEN = os.getenv("LINE_MESSAGE_CHANNEL_TOKEN", "")
LINE_MESSAGE_TO = os.getenv("LINE_MESSAGE_TO", "")
LINE_MESSAGE_API = "https://api.line.me/v2/bot/message/push"


# ────────────────────────────────────────────────────────
# 【環境切り替えスイッチ】
# ────────────────────────────────────────────────────────
IS_DEMO = True  # True: 検証用（デモ） / False: 本番環境


# ────────────────────────────────────────────────────────
# 【各環境の詳細設定】
# ────────────────────────────────────────────────────────
if IS_DEMO:
    API_PORT = os.getenv("API_PORT_DEV", "18081")
    API_PASSWORD = os.getenv("API_PASSWORD_DEV", "")
    print("[MODE] 検証用（デモ）環境として読み込みました。")
else:
    API_PORT = os.getenv("API_PORT_PRD", "18080")
    API_PASSWORD = os.getenv("API_PASSWORD_PRD", "")
    print("[MODE] 本番環境として読み込みました！")

BASE_URL = f"http://localhost:{API_PORT}/kabusapi"
