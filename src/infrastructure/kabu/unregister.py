import logging

from src.api import request_handler
from src.config import config

logger = logging.getLogger(__name__)


def unregister_all(token: str) -> dict | None:
    """kabuステーションAPIの登録銘柄をすべて解除します。"""
    url = f"{config.BASE_URL}/unregister/all"
    logger.info("銘柄登録を全解除します: %s", url)
    response = request_handler.send_put(url, headers={"X-API-KEY": token})
    if response is None:
        logger.error("銘柄登録の全解除に失敗しました。後続処理を中止します。")
        return None

    registered = response.get("RegistList", []) if isinstance(response, dict) else []
    logger.info("銘柄登録を全解除しました: 解除後の登録数=%d", len(registered))
    return response