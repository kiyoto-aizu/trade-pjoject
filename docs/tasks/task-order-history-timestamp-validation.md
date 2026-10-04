# 改修タスク: 注文履歴timestampの検証

- 起票日: 2026-10-04
- 対象: `src/domain/models.py`、`src/domain/rules.py`、`src/application/trading_usecase.py`、注文履歴関連テスト
- ステータス: **未着手**
- 実施時期: **未定（優先度は別途判断）**
- 関連調査: 2026-10-02「売買部分サイレントスキップ横並び調査」。第1弾の理由コード一覧にtimestamp異常の専用コードはない。時間ロック側にはdebugログのみ存在する。

## 背景

注文履歴のtimestampが不正な場合、当日重複判定と短時間ロック判定がその注文行を判定対象から外す。該当銘柄・方向の履歴があってもガードが有効にならず、二重注文防止をすり抜け得る。

## 現行コードで確認した挙動

| 箇所 | 実装・位置 | 現行挙動 |
|---|---|---|
| JSONからの復元 | `OrderHistoryEntry.from_dict()`（`src/domain/models.py:60,75`） | timestampを文字列として代入するだけで、ISO形式を検証しない。不正文字列も履歴へロードされる。timestampキー欠落は`KeyError`となる。 |
| 当日重複判定 | `is_duplicate_order()` / `_entry_date()`（`src/domain/rules.py:130,153,164`） | `_entry_date()`は`datetime.fromisoformat()`の`ValueError`を`None`にし、該当注文を同日注文として数えない。timestamp不正の警告・理由記録はない。 |
| 短時間ロック | `is_recent_order()`（`src/domain/rules.py:173,196,200`） | 不正なtimestampはdebugログを出して行をskipし、ロック中注文として数えない。 |
| 呼出し側 | `TradingUseCase.run()`（`src/application/trading_usecase.py:1924,1952`） | `is_safe_to_order()`が当日重複と直近注文を確認するが、上記の不正timestamp行は両判定で有効な時刻として扱われない。 |

既存の注文履歴テストは正しいISO timestampとJSON破損時の読込停止を確認する。不正文字列timestampが重複/ロック判定をすり抜けるケースは確認できない。文字列以外（`None`や数値）のtimestampは`datetime.fromisoformat()`が`TypeError`となる可能性があり、現在の捕捉範囲との相互作用は未確認。

## 必須対応

- 注文履歴のtimestamp形式・日時としての妥当性を検出し、不正行を無言で重複判定・時間ロック判定から除外しない。
- 異常検出をログ・理由コード等で記録し、現行の第1弾取引理由コード体系に沿った分類を定める。
- 不正行の扱いを選択した後、同じ銘柄・方向の重複注文を不正timestampによって通してしまわないことを保証する。

## 要判断

1. 不正行を無視する、行を除外して警告する、または注文履歴全体をfail-fastで読み込み停止する、のどれにするか。
2. 妥当なtimestampの仕様（ISO 8601、timezone必須か、naive datetimeを許すか、未来日時をどうするか）を決める。
3. timestampが不正でも同日判定だけ別情報から保守的に行うか、同一銘柄・方向の全注文をロックするかを決める。
4. 欠落・文字列不正・timezone不正・型不正を同じ理由コードに集約するか分けるかを判断する。

## 売買結果への影響

**変わりうる。** 不正行を重複とみなす、または起動停止する場合、現行なら通っていた注文が拒否・遅延され、約定数・損益に影響する。逆に記録の修復だけで終わる設計も可能だが、最終方針次第で**40件カウントの起点**が変わり得る。

## 完了条件

- 有効ISO、malformed文字列、欠落値、timezoneなし、未来日時、非文字列型を分けたテストがある。
- 不正timestampの注文と同じ銘柄・方向の新規注文について、当日重複・時間ロックの各方針をテストする。
- 既存の`test_order_history_load_corrupt`等、JSON破損時の起動停止仕様と混同せず、決定した行単位/ファイル単位ポリシーを確認する。
- 日時依存テストでは注入時計を用い、実時計に依存しない。
- 実kabu注文を行うテストは作成しない。

## 関連資料

- 第1弾の判断理由記録: `src/infrastructure/persistence/decision_journal_repository.py`の`TRADING_REASON_CODES`
- 注文履歴破損時の既存テスト: `tests/test_trading_bot.py::test_order_history_load_corrupt`