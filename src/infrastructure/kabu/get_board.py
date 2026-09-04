"""infrastructure/kabu/get_board.py"""
import logging
from src.infrastructure.kabu.board_repository import BoardRepository

logger = logging.getLogger(__name__)


def get_current_board(token, symbol):
    """
    kabuステーションの/board APIから現在の株価情報を取得する関数。
    実体はBoardRepositoryに委譲します（重複実装を避けるための薄いラッパー）。
    """
    board = BoardRepository(token).get_current_board(symbol)
    if board:
        logger.info("📊 [板情報取得] 銘柄: %s | 現在値: %s", symbol, board.get('current_price'))
    return board