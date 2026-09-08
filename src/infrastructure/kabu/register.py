import logging

from src.api import request_handler
from src.config import config

logger = logging.getLogger(__name__)


def register_symbols(token: str, symbols: list[str]) -> dict | None:
    """指定銘柄を登録し、APIが返した登録一覧をログ出力します。"""
    payload = {"Symbols": [{"Symbol": symbol, "Exchange": 1} for symbol in symbols]}
    response = request_handler.send_put(
        f"{config.BASE_URL}/register",
        data=payload,
        headers={"X-API-KEY": token},
    )
    if response is None:
        logger.error("銘柄登録に失敗しました: 対象=%s", symbols)
        return None
    registered = response.get("RegistList", []) if isinstance(response, dict) else []
    logger.info(
        "銘柄登録一覧: バッチ対象=%d件 | 登録済み=%d件 | 銘柄=%s",
        len(symbols), len(registered), [item.get("Symbol") for item in registered],
    )
    return response