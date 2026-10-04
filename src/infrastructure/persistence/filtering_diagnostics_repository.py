import json
from datetime import datetime
from pathlib import Path


class FilteringDiagnosticsRepository:
    """評価ごとのフィルタ詳細を本体結果とは別ファイルに追記保存します。"""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def save(self, diagnostics: dict) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%H%M%S%f")
        path = self.directory / f"{diagnostics['date']}_{timestamp}.json"
        path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def load_latest_for_date(self, target_date: str) -> dict | None:
        """指定日の診断ファイルから最も新しい記録を読み込みます。"""
        paths = sorted(self.directory.glob(f"{target_date}_*.json"), reverse=True)
        for path in paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("date") == target_date:
                return data
        return None