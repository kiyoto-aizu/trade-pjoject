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
| JSONからの復元 | `OrderHistoryEntry.from_dict()`（`src/domain/models.py:60,75`） | timestampを検査せず`data['timestamp']`をそのまま代入する。不正文字列・`None`・数値も復元時点では拒否しない。 |
| 当日重複判定 | `is_duplicate_order()` / `_entry_date()`（`src/domain/rules.py:130,153,164`） | `_entry_date()`は`ValueError`だけを捕捉して`None`を返し、不正文字列を「当日ではない」扱いにする。`None`や数値は`datetime.fromisoformat()`が`TypeError`を送出し、未捕捉のまま判定が落ちる。 |
| 短時間ロック | `is_recent_order()`（`src/domain/rules.py:173,196,200`） | `ValueError`だけを捕捉し、debugログ後に行をskipする。`None`や数値による`TypeError`は未捕捉で判定が落ちる。 |
| 呼出し側 | `TradingUseCase.run()`（`src/application/trading_usecase.py:1924,1952`） | `is_safe_to_order()`が当日重複と直近注文を確認する。不正文字列timestampは当日重複・時間ロックの対象から外れ、非文字列timestampは`TypeError`で処理を中断し得る。 |

既存の注文履歴テストは正しいISO timestampとJSON破損時の読込停止を確認する。不正文字列timestampのすり抜け、`None`/数値の`TypeError`、timezone混在は既存テストで確認できない。

## 必須対応

- 注文履歴のtimestamp形式・日時としての妥当性を検出し、不正行を無言で重複判定・時間ロック判定から除外しない。
- 異常検出をログ・理由コード等で記録し、現行の第1弾取引理由コード体系に沿った分類を定める。
- 不正行の扱いを選択した後、同じ銘柄・方向の重複注文を不正timestampによって通してしまわないことを保証する。

## 決定事項

1. **起動時に履歴全行のtimestampを検査し、不正があれば停止する**: `_load_order_history()`で全行を検証する。既存の読込破損が`ValueError`で停止する設計に揃え、不正行の位置と中身をログ・Slackへ出してfail-fastにする。実行中に追加される行はシステム自身が生成するため、起動時検査を基本とする。
2. **判定側の握りつぶしを理由コード付きwarningにする**: `_entry_date()`と`is_recent_order()`は`ValueError`に加えて`TypeError`も捕捉し、debugのみで無言に無効化しない。理由コード名の案は`ORDER_HISTORY_TIMESTAMP_INVALID`。第1弾の`ORDER_REJECTED_*`等と同じ大文字snake-caseで記録し、名称の確定は実装時に行う。

## 確認事項

- `is_recent_order()`は`ts >= cutoff`で比較する。実行確認では、timezone付きtimestampとtimezoneなしの`cutoff`を比較するとPythonが`TypeError`を送出する。naive/awareを許容・正規化する方針は実装時に決める。

## 売買結果への影響

**変わりうる。** 不正行を重複とみなす、または起動停止する場合、現行なら通っていた注文が拒否・遅延され、約定数・損益に影響する。逆に記録の修復だけで終わる設計も可能だが、最終方針次第で**40件カウントの起点**が変わり得る。

## 完了条件

- 有効ISO、malformed文字列、欠落値、timezoneなし、未来日時、非文字列型を分けたテストがある。
- 不正timestampの行を含む履歴で起動すると停止し、不正行の位置と内容がログ・Slackに出る。
- timestampが`None`・数値の行でも判定中に処理全体が落ちず、`ORDER_HISTORY_TIMESTAMP_INVALID`（または実装時に確定した同系コード）のwarningが出る。
- 正しいtimestamp履歴での起動、当日重複判定、時間ロックの挙動は変わらない。
- 不正timestampの注文と同じ銘柄・方向の新規注文について、当日重複・時間ロックの各方針をテストする。
- 既存の`test_order_history_load_corrupt`等、JSON破損時の起動停止仕様と混同せず、決定した行単位/ファイル単位ポリシーを確認する。
- 日時依存テストでは注入時計を用い、実時計に依存しない。
- 実kabu注文を行うテストは作成しない。

## 関連資料

- 第1弾の判断理由記録: `src/infrastructure/persistence/decision_journal_repository.py`の`TRADING_REASON_CODES`
- 注文履歴破損時の既存テスト: `tests/test_trading_bot.py::test_order_history_load_corrupt`