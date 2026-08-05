import requests


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
    except Exception as e:
        print(f"⚠️ Yahooの履歴取得に失敗しました: {e}")
        return []


def sample_yfinance_analysis(symbol):
    print(f"📡 Yahoo Financeから {symbol} の本物の過去データを取得中...")

    closes = get_yahoo_5d_closes(symbol)
    print("\n📊 【取得したデータ（直近5日分）】")
    print(f"終値: {closes}")
    print("-" * 50)

    if not closes:
        print("⚠️ 過去5日分の終値データを取得できませんでした。Yahoo の応答が空でした。")
        return

    moving_average = sum(closes) / len(closes)
    print(f"📈 過去5日間の終値一覧: {closes}")
    print(f"🧮 5日平均価格（移動平均）: {moving_average:.1f} 円")

    target_buy = round(moving_average * 0.99, 1)
    target_sell = round(moving_average * 1.01, 1)
    print(f"👉 【AI自動計算結果】 買い基準: {target_buy}円以下 / 売り基準: {target_sell}円以上")


if __name__ == "__main__":
    test_yfinance_analysis("7203")