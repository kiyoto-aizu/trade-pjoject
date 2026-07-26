"""静的スクリーニングの雛形。

実際の取得ロジック（API/スクレイピング/CSV）は実装箇所に置換してください。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import random
from pathlib import Path

from common.storage import write_json
import config

DATA_DIR = Path(__file__).resolve().parents[2] / 'data'
DATA_DIR.mkdir(exist_ok=True)

def fetch_universe():
    # ここを実データ取得に差し替える
    # テスト用にダミーの銘柄コードを生成
    universe = [str(1000 + i) for i in range(100)]
    return universe

def score_and_filter(universe):
    # 売買代金やボラティリティでフィルタするダミー実装
    scored = []
    for s in universe:
        score = random.random()
        scored.append({'symbol': s, 'score': score})
    scored.sort(key=lambda x: x['score'], reverse=True)
    # 上位 N を返す
    top = [entry['symbol'] for entry in scored[:50]]
    return top

def run(output_path: Path = DATA_DIR / 'screened_symbols.json'):
    universe = fetch_universe()
    screened = score_and_filter(universe)
    write_json(output_path, screened)
    print(f"✅ screening finished: {len(screened)} symbols -> {output_path}")


if __name__ == '__main__':
    run()
