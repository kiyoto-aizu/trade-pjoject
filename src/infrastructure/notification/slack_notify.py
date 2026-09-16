import logging
from contextlib import contextmanager
import requests
from src.config import config

logger = logging.getLogger(__name__)


def notify_process_start(process_name: str, detail: str = "開始") -> None:
    """処理開始を通知します（dailyチャンネル）。通知失敗で本処理を中断させません。"""
    _send_process_message(f"【{process_name}】{detail}", critical=False)


def notify_process_end(process_name: str, success: bool = True, detail: str = "") -> None:
    """処理終了を通知します。異常終了時はcriticalチャンネルに振り分けます。"""
    status = "終了" if success else "異常終了"
    message = f"【{process_name}】{status}"
    if detail:
        message += f"\n{detail}"
    _send_process_message(message, critical=not success)


def _send_process_message(message: str, critical: bool) -> None:
    try:
        if critical:
            notify_critical(message)
        else:
            notify_daily(message)
    except Exception:
        logger.exception("処理状態通知に失敗しました。")


def _analyze_exception_safely(process_name: str, exc: BaseException):
    try:
        from src.infrastructure.analysis.error_analyzer import create_error_analyzer

        analyzer = create_error_analyzer()
        if not analyzer:
            return None
        return analyzer.analyze_exception(process_name, exc)
    except Exception:
        logger.exception("LLM例外分析の呼び出し自体に失敗しました。")
        return None


def _short_summary(summary: str, limit: int = 200) -> str:
    """Slack通知向けに、深夜のエラー洪水でも一目で判断できる短い要約に切り詰める。"""
    lines = [line for line in summary.strip().splitlines() if line.strip()]
    short = "\n".join(lines[:2])
    return short if len(short) <= limit else short[:limit] + "…"


@contextmanager
def process_notification(
    process_name: str,
    notify_lifecycle: bool = True,
    trigger: str = "",
    start_detail: str = "開始",
):
    """処理の開始と終了を通知します。例外は呼び出し元へ再送出します。"""
    trigger_detail = f": 契機={trigger}" if trigger else ""
    logger.info("【%s】%s%s", process_name, start_detail, trigger_detail)
    if notify_lifecycle:
        notify_process_start(process_name, start_detail)
    try:
        yield
    except KeyboardInterrupt:
        raise
    except (Exception, SystemExit) as exc:
        logger.exception("%s処理が予期しないエラーで終了しました", process_name)
        analysis = _analyze_exception_safely(process_name, exc)
        detail = ""
        if analysis:
            logger.info(
                "LLM例外原因分析(参考・%d回目):\n%s", analysis.occurrence_count, analysis.summary,
            )
            headline = f"【{type(exc).__name__}】{analysis.occurrence_count}回目"
            detail = f"{headline}\n--- LLM原因分析（参考） ---\n{_short_summary(analysis.summary)}"
        if notify_lifecycle:
            notify_process_end(process_name, success=False, detail=detail)
        logger.info("【%s】異常終了%s", process_name, trigger_detail)
        raise
    else:
        if notify_lifecycle:
            notify_process_end(process_name)
        logger.info("【%s】終了%s", process_name, trigger_detail)


def _send_slack_notify(message: str, webhook_url: str, channel_label: str) -> bool:
    if config._is_test_runtime():
        logger.info("テスト実行中のためSlack通知を抑止しました(%s): %s", channel_label, message)
        return False

    if not webhook_url:
        logger.warning("⚠️ Slack Webhook URLが設定されていません(%s)。config.SLACK_WEBHOOK_%s を確認してください。", channel_label, channel_label.upper())
        return False

    payload = {"text": message}
    try:
        response = requests.post(webhook_url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        response_text = None
        if hasattr(e, 'response') and getattr(e, 'response') is not None:
            response_text = getattr(e.response, 'text', None)
        if response_text:
            logger.error("❌ Slack送信失敗(%s): %s | レスポンス: %s", channel_label, e, response_text)
        else:
            logger.error("❌ Slack送信失敗(%s): %s", channel_label, e)
        return False


def notify_critical(message: str) -> bool:
    """約定・キルスイッチ・例外など、即座に確認すべき通知を送信します。"""
    return _send_slack_notify(message, config.SLACK_WEBHOOK_CRITICAL, "critical")


def notify_daily(message: str) -> bool:
    """スクリーニング/フィルタリング結果、開始終了、日次レポートなど定型の通知を送信します。"""
    return _send_slack_notify(message, config.SLACK_WEBHOOK_DAILY, "daily")


def notify_analysis(message: str) -> bool:
    """週次・月次分析、バックテスト、分足バックフィルなど振り返り用の通知を送信します。"""
    return _send_slack_notify(message, config.SLACK_WEBHOOK_ANALYSIS, "analysis")

# def notify_weekly(message: str) -> bool:
#     """週次の振り返り用の通知を送信します。"""
#     return _send_slack_notify(message, config.SLACK_WEBHOOK_WEEKLY, "weekly")

# def notify_monthly(message: str) -> bool:
#     """月次の振り返り用の通知を送信します。"""
#     return _send_slack_notify(message, config.SLACK_WEBHOOK_MONTHLY, "monthly")
