"""
================================================================================
ドメインモデル定義モジュール
取引システムで使用される主要なデータクラスを定義しています。
================================================================================
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from src.domain.enums import OrderSide, RankingType


# ================================================================================
# 注文履歴
# ================================================================================

@dataclass
class OrderHistoryEntry:
    """
    実行した注文の履歴を記録するデータクラス。
    
    Attributes:
        symbol: 株式シンボル（銘柄コード）
        side: 注文種別（買い/売り）
        price: 約定価格
        qty: 注文数量
        timestamp: 注文時刻（ISO形式）
        result_code: kabu APIの発注応答コード（Result。0が成功）
        order_id: kabu APIが発行した注文受付番号（OrderId）
        basis_buy_limit: 発注根拠となった買い基準値（PriceLimit.buy）
        basis_sell_limit: 発注根拠となった売り基準値（PriceLimit.sell）
    """
    symbol: str
    side: OrderSide
    price: float
    qty: int
    timestamp: str
    result_code: Optional[int] = None
    order_id: Optional[str] = None
    basis_buy_limit: Optional[float] = None
    basis_sell_limit: Optional[float] = None

    @classmethod
    def from_dict(cls, data: Dict) -> 'OrderHistoryEntry':
        """
        辞書からOrderHistoryEntryインスタンスを生成します。
        
        Args:
            data: 注文履歴データを含む辞書
            
        Returns:
            OrderHistoryEntryインスタンス
        """
        return cls(
            symbol=data['symbol'],
            side=OrderSide(data['side']),
            price=data['price'],
            qty=data['qty'],
            timestamp=data['timestamp'],
            result_code=data.get('result_code'),
            order_id=data.get('order_id'),
            basis_buy_limit=data.get('basis_buy_limit'),
            basis_sell_limit=data.get('basis_sell_limit'),
        )

    def to_dict(self) -> Dict:
        """
        OrderHistoryEntryを辞書形式に変換します。
        
        Returns:
            辞書形式の注文履歴データ
        """
        return {
            'symbol': self.symbol,
            'side': self.side.value,
            'price': self.price,
            'qty': self.qty,
            'timestamp': self.timestamp,
            'result_code': self.result_code,
            'order_id': self.order_id,
            'basis_buy_limit': self.basis_buy_limit,
            'basis_sell_limit': self.basis_sell_limit,
        }


# ================================================================================
# 価格設定
# ================================================================================

@dataclass
class PriceLimit:
    """
    銘柄ごとの買い/売り基準値を保持するデータクラス。
    
    Attributes:
        buy: 買い基準値（この価格以下なら買い）
        sell: 売り基準値（この価格以上なら売り）
    """
    buy: float
    sell: float


# ================================================================================
# 取引シグナル
# ================================================================================

@dataclass
class TradeSignal:
    """
    買い/売りシグナルを表すデータクラス。
    
    Attributes:
        symbol: 株式シンボル（銘柄コード）
        side: 注文種別（買い/売り）
        price: シグナル発生時の価格
        qty: 推奨注文数量
    """
    symbol: str
    side: OrderSide
    price: float
    qty: int

    @classmethod
    def evaluate(cls, symbol: str, current_price: float, limit: PriceLimit) -> Optional['TradeSignal']:
        """
        現在価格と基準値を比較して、取引シグナルを生成します。
        
        Args:
            symbol: 株式シンボル
            current_price: 現在価格
            limit: 買い/売り基準値
            
        Returns:
            TradeSignalインスタンス（シグナルがない場合はNone）
        """
        if current_price <= limit.buy:
            return cls(symbol=symbol, side=OrderSide.BUY, price=current_price, qty=100)
        if current_price >= limit.sell:
            return cls(symbol=symbol, side=OrderSide.SELL, price=current_price, qty=100)
        return None

    def to_order_history_entry(self, limit: 'PriceLimit', order_response: Optional[Dict] = None) -> 'OrderHistoryEntry':
        """
        TradeSignalを注文履歴エントリに変換します。
        
        Args:
            limit: シグナル生成の根拠となった買い/売り基準値
            order_response: kabu APIの発注応答（Result, OrderIdを含む）
            
        Returns:
            OrderHistoryEntryインスタンス
        """
        order_response = order_response or {}
        return OrderHistoryEntry(
            symbol=self.symbol,
            side=self.side,
            price=self.price,
            qty=self.qty,
            timestamp=datetime.now().isoformat(),
            result_code=order_response.get('Result'),
            order_id=order_response.get('OrderId'),
            basis_buy_limit=limit.buy,
            basis_sell_limit=limit.sell,
        )


# ================================================================================
# ランキング情報
# ================================================================================

@dataclass
class RankingEntry:
    """
    銘柄のランキング情報を保持するデータクラス。
    
    Attributes:
        symbol: 株式シンボル（銘柄コード）
        rank: ランキング順位
        value: ランキング値（出来高、値上がり率など）
        ranking_type: ランキング種別
    """
    symbol: str
    rank: int
    value: float
    ranking_type: RankingType
    current_price: float | None = None


# ================================================================================
# 規制情報
# ================================================================================

@dataclass
class Regulation:
    """
    銘柄の規制・制限情報を保持するデータクラス。
    
    Attributes:
        symbol: 株式シンボル（銘柄コード）
        is_restricted: 取引が制限されているかどうか
        reason: 制限理由
        primary_exchange: プライマリ取引所コード
    """
    symbol: str
    is_restricted: bool
    reason: str = ""
    primary_exchange: int = 1


@dataclass
class ExclusionResult:
    """候補除外の結果。"""
    remaining: list[str]
    excluded_by_price_count: int = 0
    excluded_by_regulation_count: int = 0
    excluded_by_exchange_count: int = 0


# ================================================================================
# スクリーニング結果
# ================================================================================

@dataclass
class ScreeningAuditEntry:
    """スクリーニング候補の採用判定を再現するための監査情報。"""
    symbol: str
    turnover_rank: int
    turnover_value: float
    price_gain_rank: int
    price_gain_value: float
    total_rank: int
    primary_exchange: int
    is_restricted: bool
    restriction_reason: str
    selected: bool


@dataclass
class ScreeningResult:
    """
    スクリーニング処理の結果を保持するデータクラス。
    
    Attributes:
        date: スクリーニング実行日（ISO形式）
        symbols: スクリーニング対象銘柄のリスト
        generated_at: 結果生成時刻（ISO形式）
        audit_entries: 全候補のランキング・規制・採用判定情報
    """
    date: str
    symbols: list[str]
    generated_at: str
    audit_entries: list[ScreeningAuditEntry] = field(default_factory=list)

    def to_dict(self) -> Dict:
        """JSON永続化用の辞書へ変換します。"""
        return {
            "date": self.date,
            "symbols": self.symbols,
            "generated_at": self.generated_at,
            "audit_entries": [entry.__dict__ for entry in self.audit_entries],
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ScreeningResult":
        """旧形式のJSONも含め、辞書から結果を復元します。"""
        return cls(
            date=data["date"],
            symbols=data["symbols"],
            generated_at=data["generated_at"],
            audit_entries=[ScreeningAuditEntry(**entry) for entry in data.get("audit_entries", [])],
        )


# ================================================================================
# スコア付き候補
# ================================================================================

@dataclass
class ScoredCandidate:
    """
    出来高などの指標でスコアリングされた候補銘柄を保持するデータクラス。
    
    Attributes:
        symbol: 株式シンボル（銘柄コード）
        today_volume: 今日の出来高
        average_volume: 平均出来高
        surge_ratio: 急騰率
    """
    symbol: str
    today_volume: float
    average_volume: float
    surge_ratio: float


# ================================================================================
# フィルタリング結果
# ================================================================================

@dataclass
class FilteringResult:
    """
    フィルタリング処理の結果を保持するデータクラス。
    
    Attributes:
        date: フィルタリング実行日（ISO形式）
        symbols: フィルタリング後の銘柄リスト
        generated_at: 結果生成時刻（ISO形式）
    """
    date: str
    symbols: list[str]
    generated_at: str
