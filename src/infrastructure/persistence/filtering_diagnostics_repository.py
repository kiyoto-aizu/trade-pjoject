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