import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def write_json(file_path: Path, data):
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except OSError:
        logger.exception("Failed to write JSON to %s", file_path)


def read_json(file_path: Path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        logger.exception("JSON decode error reading %s", file_path)
        return None
    except OSError:
        logger.exception("Failed to read JSON from %s", file_path)
        return None