import json
import logging

import requests

from src.config import config

logger = logging.getLogger(__name__)


def _build_period_review_prompt(period_label: str, period_summary: dict) -> str:
    return (
        f"以下は自動売買システムの{period_label}集計です。数値を再計算せず、入力にある事実だけを根拠に、"
        "次の検証につながる短い日本語レビューを作成してください。全体を900字以内にしてください。"
        "見出しは必要なものだけを使い、各見出しの箇条書きは最大2件にしてください。"
        "「観測事実」は必ず使い、期間、ペーパートレード、バックテスト、確定済み判定イベントの重要な事実だけを書いてください。"
        "period.statusがcomplete以外の場合だけ「期間・稼働状態」を使い、途中集計または未開始であることを明記してください。"
        "この場合、未到来のレポートやバックテストがないことを障害やスケジュール不備と推測しないでください。"
        "daily.operational_summaryにエラー、緊急停止、unavailable、not_evaluated、not_recordedがある場合だけ、"
        "同見出しで取引・戦略を評価できる状態だったかを事実として示してください。"
        "backtest.exact_period_run_availableがtrueかつperiod.statusがcompleteの場合だけ、対象期間のバックテスト結果を評価してください。"
        "それ以外ではbacktest.runsの損益・取引数を合算せず、ペーパートレードとの乖離や成績比較を結論づけないでください。"
        "comparison.availableがtrueの場合だけ「期間比較」を使い、comparison.basisと両期間のreport_countを併記してください。"
        "filter_decision_events.countが1件以上の場合だけ「判定イベント」を使い、概算損益を扱う場合は実取引損益ではないと明記してください。"
        "daily.total_profit_lossは日次レポートの記録値であり、内訳がない限り実現損益・未実現損益を断定しないでください。"
        "過学習はアウトオブサンプル結果やパラメータ比較などの根拠がない限り断定しないでください。"
        "仮説を書く場合は「仮説」と明示し、入力に直接根拠がある場合だけにしてください。"
        "未解決事項を判定できる具体的な観測値またはログがある場合だけ、「次回確認」を1件書いてください。"
        "ロジック変更、認証情報の修正、投資判断、売買指示は行わず、検証期間中の参考意見だと明記してください。\n\n"
        f"{period_label}集計:\n{json.dumps(period_summary, ensure_ascii=False, indent=2)}"
    )


class OpenAIDailyAnalyzer:
    """日次取引とバックテストの集計をLLMで評価するアダプター。"""

    def __init__(self, api_key: str, model: str, api_url: str, timeout: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.timeout = timeout

    def analyze(self, daily_summary: dict) -> str | None:
        prompt = (
            "以下は自動売買の1日分の機械集計です。数値を再計算せず、入力にある事実だけを根拠に、"
            "翌日の検証につながる短い日本語レビューを作成してください。全体を600字以内にしてください。"
            "見出しは必要なものだけを使い、各見出しの箇条書きは最大2件にしてください。"
            "「本日の事実」は必ず使い、注文数、損益、保有、market_conditions、log_errorsから重要な事実だけを書いてください。"
            "market_conditions.assessment_statusがnot_evaluatedまたはunavailable、またはlog_errors.has_errorsがtrueの場合だけ、"
            "「運用・データ状態」を使い、取引・戦略を評価できる状態だったかを事実として示してください。"
            "注文、見送り、損切り、緩和、強制決済など、日次集計内のイベント配列が空でない場合だけ「戦略イベント」を使い、"
            "結果を最大2件に集約してください。空配列や発生しなかったイベントは本文で説明しないでください。"
            "当日の未解決事項を判定できる具体的な観測値またはログがある場合だけ、「次回確認」を1件書いてください。"
            "仮説を書く場合は「仮説」と明示し、入力に直接根拠がある場合だけにしてください。"
            "assessment_statusがnot_evaluatedは市場判定が未実行であり、市場データ取得失敗とは書かないでください。"
            "assessment_statusがunavailableでも、failure_reasonまたはエラー概要にない障害原因を断定しないでください。"
            "認証エラーと市場データ未取得など、複数事象の因果関係も入力に根拠がなければ断定しないでください。"
            "見送り・損切り後の観測損益を扱う場合は、観測終了時刻までの概算であり実取引損益ではないと明記してください。"
            "ロジック変更、認証情報の修正、投資判断、売買指示は行わず、検証期間中の参考意見だと明記してください。\n\n"
            f"日次集計:\n{json.dumps(daily_summary, ensure_ascii=False, indent=2)}"
        )
        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "あなたは自動売買システムの日次レビュー担当です。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に本文がありません")
            return content.strip()[:1800]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("日次LLM分析に失敗しました: %s", exc)
            return None

    def analyze_backtest(self, backtest_summary: dict) -> str | None:
        prompt = (
            "以下は自動売買戦略のバックテスト集計です。与えられた数値だけを根拠に、"
            "日本語で短く評価してください。出力は「結果の評価」「注意点・不確実性」"
            "「次回の確認事項」の3見出し、各見出し2項目以内としてください。"
            "過学習、取引数不足、手数料・スリッページ、未実現損益の影響が判断できる場合は指摘し、"
            "ロジック変更や投資判断を命令しないでください。検証期間中の参考意見だと明記してください。\n\n"
            f"バックテスト集計:\n{json.dumps(backtest_summary, ensure_ascii=False, indent=2)}"
        )
        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "あなたは自動売買システムのバックテストレビュー担当です。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に本文がありません")
            return content.strip()[:1800]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("バックテストLLM分析に失敗しました: %s", exc)
            return None

    def analyze_monthly(self, monthly_summary: dict) -> str | None:
        prompt = _build_period_review_prompt("月次", monthly_summary)
        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "あなたは自動売買システムの月次レビュー担当です。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に本文がありません")
            return content.strip()[:2400]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("月次LLM分析に失敗しました: %s", exc)
            return None

    def analyze_weekly(self, weekly_summary: dict) -> str | None:
        prompt = _build_period_review_prompt("週次", weekly_summary)
        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "あなたは自動売買システムの週次レビュー担当です。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に本文がありません")
            return content.strip()[:2400]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("週次LLM分析に失敗しました: %s", exc)
            return None


def create_daily_analyzer() -> OpenAIDailyAnalyzer | None:
    if not config.LLM_DAILY_ANALYSIS_ENABLED or not config.LLM_API_KEY:
        return None
    return OpenAIDailyAnalyzer(
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        api_url=config.LLM_API_URL,
    )