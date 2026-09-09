"""infrastructure/kabu/get_board.py"""
from src.infrastructure.kabu.board_repository import BoardRepository


def get_current_board(token, symbol):
    """
    kabuステーションの/board APIから現在の株価情報を取得する関数。
    実体はBoardRepositoryに委譲します（重複実装を避けるための薄いラッパー）。
    """
    return BoardRepository(token).get_current_board(symbol)