"""バックテスト結果の保存と比較用要約。"""
import json
from datetime import datetime
from pathlib import Path


def comparison_summary(result: dict) -> dict:
    signals = result.get("signals", [])
    return {
        "total_pnl": result.get("total_pnl", 0.0),
        "total_trades": result.get("total_trades", 0),
        "final_position": result.get("final_position", 0),
        "cash": result.get("cash", 0.0),
        "volatility_adjustment": result.get("volatility_adjustment", {}),
        "atr_stop_loss_count": sum(1 for signal in signals if signal.get("note") == "atr_stop_loss"),
        "fixed_stop_loss_count": sum(1 for signal in signals if signal.get("note") == "stop_loss"),
        "trade_history": result.get("trade_history", []),
    }


def market_regime_comparison_summary(baseline: dict, enabled: dict) -> dict:
    return {
        "baseline": {
            "total_pnl": baseline.get("total_pnl", 0.0),
            "total_trades": baseline.get("total_trades", 0),
            "max_drawdown": baseline.get("max_drawdown", 0.0),
        },
        "with_market_regime": {
            "total_pnl": enabled.get("total_pnl", 0.0),
            "total_trades": enabled.get("total_trades", 0),
            "max_drawdown": enabled.get("max_drawdown", 0.0),
        },
        "delta": {
            "total_pnl": round(enabled.get("total_pnl", 0.0) - baseline.get("total_pnl", 0.0), 2),
            "total_trades": enabled.get("total_trades", 0) - baseline.get("total_trades", 0),
            "max_drawdown": round(enabled.get("max_drawdown", 0.0) - baseline.get("max_drawdown", 0.0), 2),
        },
        "market_regime_adjustment": enabled.get("market_regime_adjustment", {}),
    }


def save_backtest_result(
    output_path: Path, result: dict, archive_directory: Path | None = None
) -> Path:
    """最新結果を保存し、同じ内容を実行時刻付きの履歴として保存します。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    output_path.write_text(serialized, encoding="utf-8")
    archive_dir = archive_directory or output_path.parent
    archive_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    archive_path = archive_dir / f"{output_path.stem}_{timestamp}{output_path.suffix}"
    archive_path.write_text(serialized, encoding="utf-8")
    return archive_path
