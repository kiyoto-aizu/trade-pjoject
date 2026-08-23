from typing import List, Dict, Optional


class AccountManager:
    def __init__(self, token: str):
        self.token = token
        self.wallet: Dict[str, Optional[float]] = {}
        self.positions: List[Dict] = []

    def refresh(self, wallet_api=None, positions_api=None) -> None:
        if wallet_api:
            self.wallet = wallet_api.get_wallet_cash(self.token) or {}
        if positions_api:
            self.positions = positions_api.get_positions(self.token) or []

    def has_holdings(self, symbol: str) -> bool:
        total_qty = 0
        for pos in self.positions:
            if pos.get('Symbol') == symbol and pos.get('Side') == '1':
                total_qty += int(pos.get('HoldQty', 0) or 0)
        return total_qty > 0
