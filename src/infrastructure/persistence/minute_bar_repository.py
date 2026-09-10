import json
import logging
from dataclasses import asdict
from datetime import date
from pathlib import Path

from src.domain.models import MinuteBar

logger = logging.getLogger(__name__)


class MinuteBarRepository:
    """
    簡易分足データを「data/minute_bars/{date}/{symbol}.json」として
    銘柄・日付ごとに保存するリポジトリ。

    1ポーリングごとにappend_barを呼ぶ想定。呼ぶたびにファイルを
    読み込み直して追記・保存するため、収集プロセスが途中で落ちても
    それまでの分足は失われない。
    """

    def __init__(self, directory: Path):
        self.directory = directory

    def _path_for(self, target_date: date, symbol: str) -> Path:
        return self.directory / target_date.isoformat() / f"{symbol}.json"

    def load_bars(self, target_date: date, symbol: str) -> list[MinuteBar]:
        path = self._path_for(target_date, symbol)
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return [MinuteBar(**bar) for bar in data.get("bars", [])]

    def append_bar(self, target_date: date, symbol: str, bar: MinuteBar) -> None:
        """
        既存の分足に1本追記して保存します。

        同じ時刻の足が既にある場合の優先度:
        - 既存が"yahoo"由来で新しい足が"poll"由来 → 精度を落とさないため上書きしない（スキップ）
        - それ以外（yahooでの更新、pollでの新規、pollでのpoll上書きなど） → 新しい足で上書き
        """
        path = self._path_for(target_date, symbol)
        path.parent.mkdir(parents=True, exist_ok=True)

        bars = self.load_bars(target_date, symbol)
        existing = next((existing for existing in bars if existing.time == bar.time), None)
        if existing is not None and existing.source == "yahoo" and bar.source == "poll":
            logger.debug(
                "既にYahoo由来の分足があるため、簡易ポーリングでの上書きをスキップしました: 銘柄=%s 時刻=%s",
                symbol, bar.time,
            )
            return

        bars = [existing for existing in bars if existing.time != bar.time]
        bars.append(bar)
        bars.sort(key=lambda existing: existing.time)

        payload = {
            "date": target_date.isoformat(),
            "symbol": symbol,
            "bars": [asdict(existing) for existing in bars],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.debug("分足を保存しました: 銘柄=%s 時刻=%s 件数=%d 取得元=%s", symbol, bar.time, len(bars), bar.source)
