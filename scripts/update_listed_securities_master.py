"""上場銘柄マスタ(data/universe/listed_securities.csv)を、JPXが公開する
東証上場銘柄一覧(data_j.xls)を使って更新するスクリプト。ADR-0001。

実行方法:
    python scripts/update_listed_securities_master.py

週次(例: 毎週土曜)での実行を想定している。東証全体のIPO・上場廃止は
月に数件〜十数件程度であり、週次更新で実用上十分と判断している
(ADR-0001参照)。

処理内容:
- JPXが公開する東証上場銘柄一覧(data_j.xls)をダウンロードする
- プライム(内国株式)・スタンダード(内国株式)・グロース(内国株式)の
  3区分のみを対象とする(ETF・REIT・PRO Market等は対象外)
- 既存の上場銘柄マスタと突き合わせ、以下を反映する
  - 新規上場: 本日の日付をlisted_fromとして行を追加する
  - 上場廃止: 該当行を削除せず、本日の日付をlisted_toとして記録する
  - 市場区分の変更(市場変更): exchange_divisionを更新する
  - 既に上場廃止済みの行はそのまま維持する(再上場は別銘柄として扱う)

注意:
- data_j.xlsには銘柄ごとの上場日そのものは含まれていないため、
  「前回このスクリプトを実行した時点のマスタ」との差分でしか
  新規上場・上場廃止を検知できない。実行間隔が空くほど、
  検知できる上場日・廃止日の精度は落ちる(検知した実行日が
  listed_from/listed_toとして記録される)。
- このスクリプトの動作確認は、ネットワーク制限のある開発環境では
  行えていない(jpx.co.jpへのアクセスが必要なため)。実際の運用環境で
  一度動作を確認してから、週次タスクとして登録すること。
"""
from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

import requests
import xlrd

logger = logging.getLogger(__name__)

DATA_J_XLS_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
MASTER_CSV_PATH = Path(__file__).resolve().parents[1] / "data" / "universe" / "listed_securities.csv"
FIELDNAMES = ["symbol", "exchange_division", "listed_from", "listed_to"]

# config.SCREENING_EXCHANGE_DIVISIONSのデフォルト値(TP/TS/TG)に合わせる
MARKET_DIVISION_TO_CODE = {
    "プライム（内国株式）": "TP",
    "スタンダード（内国株式）": "TS",
    "グロース（内国株式）": "TG",
}


def fetch_current_listing(timeout: float = 30.0) -> dict[str, str]:
    """JPXのdata_j.xlsから、対象市場区分の銘柄コード→区分コードの辞書を取得する。"""
    response = requests.get(DATA_J_XLS_URL, timeout=timeout)
    response.raise_for_status()
    return parse_data_j_xls(response.content)


def parse_data_j_xls(xls_bytes: bytes) -> dict[str, str]:
    """data_j.xlsのバイト列を解析し、対象市場区分の銘柄コード→区分コードの辞書を返す。

    ネットワーク越しの取得部分(fetch_current_listing)と分離しているのは、
    ダウンロード済みのバイト列さえあればテストできるようにするため。
    """
    workbook = xlrd.open_workbook(file_contents=xls_bytes)
    sheet = workbook.sheet_by_index(0)
    header = [str(cell.value).strip() for cell in sheet.row(0)]
    try:
        code_col = header.index("コード")
        division_col = header.index("市場・商品区分")
    except ValueError as exc:
        raise RuntimeError(
            f"data_j.xlsの列構成が想定と異なります(見つかった列: {header})。"
            "JPX側でフォーマットが変わっていないか確認してください。"
        ) from exc

    current: dict[str, str] = {}
    for row_index in range(1, sheet.nrows):
        row = sheet.row(row_index)
        raw_code = str(row[code_col].value).strip()
        raw_division = str(row[division_col].value).strip()
        code = MARKET_DIVISION_TO_CODE.get(raw_division)
        if code is None or not raw_code:
            continue
        if raw_code.endswith(".0"):
            # xlrdが数値セルとして読み込んだ場合(例: "7203.0")の整形
            raw_code = raw_code[:-2]
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
