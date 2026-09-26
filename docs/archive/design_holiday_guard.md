# 設計書: 祝日における不正実行の修正

## 1. 背景・問題

祝日にもかかわらず、フィルタリング(`run_filtering.py`)・スクリーニング(`run_screening.py`)・取引ループ(`run_trading.py`)が実行されてしまう不具合が発生した。

## 2. 原因

リポジトリ調査の結果、以下の2種類の欠陥が組み合わさっていることが判明した。

| エントリポイント | 稼働日チェック | 問題点 |
|---|---|---|
| `run_screening.py` | なし | ガード自体が存在せず、無条件に実行される |
| `run_filtering.py` | なし | 同上 |
| `run_trading.py` | `is_trading_session()`あり | `src/domain/rules.py`の実装が`now.weekday() >= 5`のみで判定しており、祝日カレンダーを一切参照していない |

```python
# src/domain/rules.py（現状）
def is_trading_session(now, open_hour, open_minute, close_hour, close_minute) -> bool:
    """平日の市場時間内かを判定します。"""
    if now.weekday() >= 5:
        return False
    ...
```

祝日判定ライブラリはdomain層ではなく、`src/infrastructure/calendar/japanese_calendar.py`から利用する。

## 3. 修正方針

1. `jpholiday`ライブラリを新規依存として追加する
2. `src/domain/rules.py`に稼働日判定の単一の関数`is_trading_day(target_date: date, holiday_checker=...) -> bool`を新設する
3. 祝日ライブラリは`src/infrastructure/calendar/japanese_calendar.py`で注入し、domain層は外部ライブラリに依存しない。`is_trading_session()`も注入された判定関数を内部で呼び出す。
4. `run_filtering.py`・`run_screening.py`の`main()`冒頭に、`is_trading_day()`による早期returnガードを追加する（`run_trading.py`と同じ「非稼働日ならログを出して終了」というパターンに揃える）

## 4. インターフェース設計

```python
# src/domain/rules.py
def is_trading_day(target_date: date, holiday_checker=None) -> bool:
    """
    指定日が株式市場の稼働日かどうかを判定します。
    土曜・日曜・祝日・年末年始(12/31, 1/2, 1/3)を非稼働日とします。
    """
    if target_date.weekday() >= 5:
        return False
    if holiday_checker is not None and holiday_checker(target_date):
        return False
    if target_date.month == 12 and target_date.day == 31:
        return False
    if target_date.month == 1 and target_date.day in (2, 3):
        return False
    return True


def is_trading_session(
    now: datetime, open_hour, open_minute, close_hour, close_minute,
    holiday_checker=None,
) -> bool:
    """稼働日かつ市場時間内かを判定します。"""
    if not is_trading_day(now.date(), holiday_checker=holiday_checker):
        return False
    session_start = time(open_hour, open_minute)
    session_end = time(close_hour, close_minute)
    return session_start <= now.time() < session_end
```

`src/infrastructure/calendar/japanese_calendar.py`が`jpholiday.is_holiday`を注入して、アプリケーションから利用する日本市場向けの公開関数を提供する。domain層は外部ライブラリに依存しない。

`run_filtering.py` / `run_screening.py`側の追加ガード（イメージ）:

```python
from datetime import date
from src.infrastructure.calendar.japanese_calendar import is_trading_day

def main() -> None:
    configure_logging()
    if not is_trading_day(date.today()):
        logging.getLogger(__name__).info('休場日のため、処理を開始しません。')
        return
    ...（既存処理）
```

## 5. 変更対象ファイル

- `requirements.txt`: `jpholiday`追加
- `src/domain/rules.py`: `is_trading_day`新設、`is_trading_session`をリファクタ
- `src/entrypoints/run_filtering.py`: 稼働日ガード追加
- `src/entrypoints/run_screening.py`: 稼働日ガード追加
- `tests/`配下: `is_trading_day`のユニットテスト、`is_trading_session`の祝日ケース追加、`run_filtering`/`run_screening`の休場日早期return確認テスト

## 6. 年末年始の扱い（決定事項）

`jpholiday`は「国民の祝日」しか判定できないため、東証休場日である年末年始(12/31, 1/2, 1/3)は別途`is_trading_day`に組み込む。1/1は`jpholiday`が元日として判定するため対象外でよい。

```python
def is_trading_day(target_date: date) -> bool:
    if target_date.weekday() >= 5:
        return False
    if jpholiday.is_holiday(target_date):
        return False
    if target_date.month == 12 and target_date.day == 31:
        return False
    if target_date.month == 1 and target_date.day in (2, 3):
        return False
    return True
```

## 7. スコープ外とする論点

- **大納会・大発会等の半日/特殊日**: 通常は終日立会があるため実害は小さく、今回は対象外とする

## 8. テスト方針

- `is_trading_day`: 平日/土日/祝日/年末年始(12/31,1/2,1/3)の4パターンでユニットテスト
- `is_trading_session`: 祝日の平日ケースを追加（現状のテストに祝日ケースが無ければ追加）
- `run_filtering.py`/`run_screening.py`: 休場日に`main()`が早期returnし、既存の処理（トークン取得・API呼び出し等）が一切実行されないことを確認するテスト
- 変更後、既存を含む全テストスイートをpytestで実行し、全件passを確認
