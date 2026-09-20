"""上場銘柄マスタ(data/universe/listed_securities.csv)を、JPXが公開する
東証上場銘柄一覧(data_j.xlsx)を使って更新するスクリプト。ADR-0001。

実行方法:
    python scripts/update_listed_securities_master.py

週次(例: 毎週土曜)での実行を想定している。東証全体のIPO・上場廃止は
月に数件〜十数件程度であり、週次更新で実用上十分と判断している
(ADR-0001参照)。

処理内容:
- JPXが公開する東証上場銘柄一覧(data_j.xlsx)をダウンロードする
- プライム(内国株式)・スタンダード(内国株式)・グロース(内国株式)の
  3区分のみを対象とする(ETF・REIT・PRO Market等は対象外)
- 既存の上場銘柄マスタと突き合わせ、以下を反映する
  - 新規上場: 本日の日付をlisted_fromとして行を追加する
  - 上場廃止: 該当行を削除せず、本日の日付をlisted_toとして記録する
  - 市場区分の変更(市場変更): exchange_divisionを更新する
  - 既に上場廃止済みの行はそのまま維持する(再上場は別銘柄として扱う)

注意:
- data_j.xlsxには銘柄ごとの上場日そのものは含まれていないため、
  「前回このスクリプトを実行した時点のマスタ」との差分でしか
  新規上場・上場廃止を検知できない。実行間隔が空くほど、
  検知できる上場日・廃止日の精度は落ちる(検知した実行日が
  listed_from/listed_toとして記録される)。
- JPXは毎月第3営業日の午前9時以降に前月末データへ差し替える運用。
  そのため月初の数日は前月末時点のデータになる。
- 2026年9月時点でJPXはこのファイルを旧形式(.xls)から.xlsxに変更済み。
  ファイル形式が今後変わった場合は本スクリプトの修正が必要になる。
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date
from pathlib import Path

import openpyxl
import requests

logger = logging.getLogger(__name__)

DATA_J_XLSX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
MASTER_CSV_PATH = Path(__file__).resolve().parents[1] / "data" / "universe" / "listed_securities.csv"
FIELDNAMES = ["symbol", "exchange_division", "listed_from", "listed_to"]

# config.SCREENING_EXCHANGE_DIVISIONSのデフォルト値(TP/TS/TG)に合わせる
MARKET_DIVISION_TO_CODE = {
    "プライム（内国株式）": "TP",
    "スタンダード（内国株式）": "TS",
    "グロース（内国株式）": "TG",
}


def fetch_current_listing(timeout: float = 30.0) -> dict[str, str]:
    """JPXのdata_j.xlsxから、対象市場区分の銘柄コード→区分コードの辞書を取得する。"""
    response = requests.get(DATA_J_XLSX_URL, timeout=timeout)
    response.raise_for_status()
    return parse_data_j_xlsx(response.content)


def parse_data_j_xlsx(xlsx_bytes: bytes) -> dict[str, str]:
    """data_j.xlsxのバイト列を解析し、対象市場区分の銘柄コード→区分コードの辞書を返す。

    ネットワーク越しの取得部分(fetch_current_listing)と分離しているのは、
    ダウンロード済みのバイト列さえあればテストできるようにするため。
    """
    workbook = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), read_only=True, data_only=True)
    sheet = workbook.worksheets[0]
    rows_iter = sheet.iter_rows(values_only=True)
    header = [str(cell).strip() if cell is not None else "" for cell in next(rows_iter)]
    try:
        code_col = header.index("コード")
        division_col = header.index("市場・商品区分")
    except ValueError as exc:
        raise RuntimeError(
            f"data_j.xlsxの列構成が想定と異なります(見つかった列: {header})。"
            "JPX側でフォーマットが変わっていないか確認してください。"
        ) from exc

    current: dict[str, str] = {}
    for row in rows_iter:
        if row is None or len(row) <= max(code_col, division_col):
            continue
        raw_code = row[code_col]
        raw_division = row[division_col]
        if raw_code is None or raw_division is None:
            continue
        code = MARKET_DIVISION_TO_CODE.get(str(raw_division).strip())
        if code is None:
            continue
        # コードが数値セルとして読まれた場合(例: 7203.0 / 7203)の整形
        if isinstance(raw_code, float):
            raw_code = str(int(raw_code))
        else:
            raw_code = str(raw_code).strip()
            if raw_code.endswith(".0"):
                raw_code = raw_code[:-2]
        if not raw_code:
            continue
        current[raw_code] = code
    return current


def load_master(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def build_updated_master(
    existing_rows: list[dict],
    current_listing: dict[str, str],
    today: str,
) -> list[dict]:
    """既存マスタと最新のJPXデータを突き合わせ、更新後の全行を返す。"""
    updated_rows: list[dict] = []
    active_symbols_in_master: set[str] = set()

    for row in existing_rows:
        symbol = row["symbol"]
        listed_to = row.get("listed_to") or None
        if listed_to:
            # 既に上場廃止済みの行はそのまま維持する
            updated_rows.append(row)
            continue
        active_symbols_in_master.add(symbol)
        if symbol in current_listing:
            new_division = current_listing[symbol]
            if row["exchange_division"] != new_division:
                logger.info(
                    "市場区分の変更を検知: 銘柄=%s | %s -> %s",
                    symbol, row["exchange_division"], new_division,
                )
                row = dict(row)
                row["exchange_division"] = new_division
            updated_rows.append(row)
        else:
            logger.info("上場廃止を検知: 銘柄=%s (listed_to=%s)", symbol, today)
            row = dict(row)
            row["listed_to"] = today
            updated_rows.append(row)

    new_symbols = sorted(set(current_listing) - active_symbols_in_master)
    for symbol in new_symbols:
        logger.info("新規上場を検知: 銘柄=%s (listed_from=%s)", symbol, today)
        updated_rows.append({
            "symbol": symbol,
            "exchange_division": current_listing[symbol],
            "listed_from": today,
            "listed_to": "",
        })

    return updated_rows


def write_master(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in FIELDNAMES})


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    today = date.today().isoformat()

    logger.info("JPXから最新の上場銘柄一覧を取得します...")
    current_listing = fetch_current_listing()
    logger.info("取得した対象銘柄数(プライム/スタンダード/グロース): %d件", len(current_listing))

    existing_rows = load_master(MASTER_CSV_PATH)
    updated_rows = build_updated_master(existing_rows, current_listing, today)

    write_master(MASTER_CSV_PATH, updated_rows)
    logger.info("上場銘柄マスタを更新しました: %s (全%d行)", MASTER_CSV_PATH, len(updated_rows))


if __name__ == "__main__":
    main()
