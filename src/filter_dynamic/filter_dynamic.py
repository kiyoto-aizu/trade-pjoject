"""場中の動的フィルタリング雛形。

このスクリプトは `data/screened_symbols.json` を読み込み、
簡易的にスコアリングして `data/top5.json` を更新します。
実際は WebSocket 等で歩み値を受けて評価してください。
"""
import json
import logging
import time
import random
from pathlib import Path

from infrastructure.persistence.storage import read_json, write_json

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parents[2] / 'data'
SCREENED = DATA_DIR / 'screened_symbols.json'
TOP5 = DATA_DIR / 'top5.json'

def score_symbols(symbols):
    scored = []
    for s in symbols:
        # 擬似スコア: ティック頻度やボラティリティを反映する箇所
        scored.append({'symbol': s, 'score': random.random()})
    scored.sort(key=lambda x: x['score'], reverse=True)
    return [entry['symbol'] for entry in scored[:5]]

def run(poll_interval: int = 10):
    while True:
        symbols = read_json(SCREENED) or []
        if not symbols:
            logger.warning('⚠️ No screened symbols found. Run `screening` first.')
            time.sleep(poll_interval)
            continue

        top5 = score_symbols(symbols)
        write_json(TOP5, top5)
        logger.info("🔄 top5 updated: %s", top5)
        time.sleep(poll_interval)


if __name__ == '__main__':
    run()
