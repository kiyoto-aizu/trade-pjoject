from src.api import request_handler
from src.config import config


def get_api_soft_limit(token: str) -> float | None:
    response = request_handler.send_get(
        f"{config.BASE_URL}/apisoftlimit",
        headers={"X-API-KEY": token},
    )
    if not response:
        return None
    value = response.get("Stock", response.get("ApisoftLimit"))
    try:
        return float(value) * 10000 if value is not None else None
    except (TypeError, ValueError):
        return None
