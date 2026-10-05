"""取引中間報告の文面を生成する純粋関数。"""
from datetime import datetime
from math import isfinite
from numbers import Real
from typing import Sequence


def _format_yen(value: object) -> str:
    try:
        if isinstance(value, bool) or not isinstance(value, Real):
            return "取得不可"
        numeric_value = float(value)
        if not isfinite(numeric_value):
            return "取得不可"
        return f"{numeric_value:+,.0f}円"
    except (TypeError, ValueError, OverflowError):
        return "取得不可"


def build_trading_progress_report(
    *,
    reported_at: datetime,
    scheduled_time: str,
    order_count: int,
    order_count_label: str,
    realized_pnl: float | None,
    holdings: Sequence[dict] | None,
    market_regime: str,
    nikkei_change_percent: float | None,
    events: Sequence[str],
    kill_switch_triggered: bool = False,
    emergency_stop_triggered: bool = False,
) -> str:
    """与えられた取引スナップショットから中間報告を組み立てます。"""
    lines = [
        "【業務】取引運用",
        f"【機能】中間報告（{scheduled_time}）",
        f"報告時刻: {reported_at:%H:%M:%S}",
        "【詳細】",
        "--- 成績 ---",
        f"{order_count_label}: {order_count}件",
        f"確定損益: {_format_yen(realized_pnl)}",
    ]

    if holdings is None:
        lines.extend(["含み損益: 取得不可", "--- 保有銘柄 ---", "保有状況: 取得不可"])
    else:
        holding_pnls = [item.get("profit_loss") for item in holdings]
        unrealized_pnl = (
            sum(float(value) for value in holding_pnls)
            if all(
                isinstance(value, Real)
                and not isinstance(value, bool)
                and isfinite(float(value))
                for value in holding_pnls
            )
            else None
        )
        lines.extend([f"含み損益: {_format_yen(unrealized_pnl)}", "--- 保有銘柄 ---"])
        if not holdings:
            lines.append("保有銘柄: なし")
        else:
            for item in holdings:
                lines.append(
                    f"{item.get('symbol', '')} {item.get('quantity', 0)}株: "
                    f"{_format_yen(item.get('profit_loss'))}"
                )

    lines.extend([
        "--- 動きの変化 ---",
        f"朝のMarketRegime: {market_regime}",
        "日経225当日値動き（前日終値比）: "
        + (f"{nikkei_change_percent:+.2f}%" if nikkei_change_percent is not None else "取得不可"),
        "--- 見送り・停止 ---",
    ])
    if kill_switch_triggered:
        events = [*events, "キルスイッチ発動"]
    if emergency_stop_triggered:
        events = [*events, "手動緊急停止"]
    lines.extend(events or (["動きなし"] if order_count == 0 else ["特記事項なし"]))
    return "\n".join(lines)