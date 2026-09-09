import logging
from contextlib import contextmanager
import requests
from src.config import config

logger = logging.getLogger(__name__)


def notify_process_start(process_name: str) -> None:
    """処理開始を通知します。通知失敗で本処理を中断させません。"""
    _send_process_message(f"【開始】{process_name}")


def notify_process_end(process_name: str, success: bool = True, detail: str = "") -> None:
    """処理終了を通知します。必要に応じて終了理由を付加します。"""
    status = "完了" if success else "失敗"
    message = f"【終了】{process_name}: {status}"
    if detail:
        message += f"\n{detail}"
    _send_process_message(message)


def _send_process_message(message: str) -> None:
    try:
        send_line_notify(message)
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
    """LINE通知向けに、深夜のエラー洪水でも一目で判断できる短い要約に切り詰める。"""
    lines = [line for line in summary.strip().splitlines() if line.strip()]
    short = "\n".join(lines[:2])
    return short if len(short) <= limit else short[:limit] + "…"


@contextmanager
def process_notification(process_name: str, notify_lifecycle: bool = True):
    """処理の開始と終了を通知します。例外は呼び出し元へ再送出します。"""
    if notify_lifecycle:
        notify_process_start(process_name)
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
        raise
    else:
        if notify_lifecycle:
            notify_process_end(process_name)


def send_line_notify(message: str) -> bool:
    if config._is_test_runtime():
        logger.info("テスト実行中のためLINE通知を抑止しました: %s", message)
        return False

    token = config.LINE_MESSAGE_CHANNEL_TOKEN
    to_user = config.LINE_MESSAGE_TO
    if not token:
        logger.warning("⚠️ LINE Message API 用チャネルアクセストークンが設定されていません。config.LINE_MESSAGE_CHANNEL_TOKEN を確認してください。")
        return False
    if not to_user:
        logger.warning("⚠️ 送信先のLINEユーザーIDが設定されていません。config.LINE_MESSAGE_TO を確認してください。")
        return False

    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}'
    }
    payload = {
        'to': to_user,
        'messages': [
            {
                'type': 'text',
                'text': message
            }
        ]
    }
    try:
        response = requests.post(config.LINE_MESSAGE_API, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        response_text = None
        if hasattr(e, 'response') and getattr(e, 'response') is not None:
            response_text = getattr(e.response, 'text', None)
        if response_text:
            logger.error("❌ LINE送信失敗: %s | レスポンス: %s", e, response_text)
        else:
            logger.error("❌ LINE送信失敗: %s", e)
        return False
