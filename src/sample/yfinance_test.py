import logging

import requests

logger = logging.getLogger(__name__)


def get_yahoo_5d_closes(symbol):
    yf_symbol = f"{symbol}.T"
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + yf_symbol
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        response = requests.get(
            url,
            params={"interval": "1d", "range": "5d"},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        result = data.get("chart", {}).get("result", [])
        if not result:
            return []

        quote = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes = [float(close) for close in quote.get("close", []) if close is not None]
        return closes[-5:]
    except (requests.RequestException, KeyError, TypeError, ValueError) as e:
        logger.warning("⚠️ Yahooの履歴取得に失敗しました: %s", e)
        return []


def sample_yfinance_analysis(symbol):
    logger.info("📡 Yahoo Financeから %s の本物の過去データを取得中...", symbol)

    closes = get_yahoo_5d_closes(symbol)
    logger.info("📊 【取得したデータ（直近5日分）】")
    logger.info("終値: %s", closes)
    logger.info("%s", "-" * 50)

    if not closes:
        logger.warning("⚠️ 過去5日分の終値データを取得できませんでした。Yahoo の応答が空でした。")
        return

    moving_average = sum(closes) / len(closes)
    logger.info("📈 過去5日間の終値一覧: %s", closes)
    logger.info("🧮 5日平均価格（移動平均）: %.1f 円", moving_average)

    target_buy = round(moving_average * 0.99, 1)
    target_sell = round(moving_average * 1.01, 1)
    logger.info("👉 【AI自動計算結果】 買い基準: %s円以下 / 売り基準: %s円以上", target_buy, target_sell)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sample_yfinance_analysis("7203")