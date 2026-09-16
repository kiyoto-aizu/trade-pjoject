from src.infrastructure.notification.slack_notify import format_result_notification


def test_format_result_notification_uses_required_sections():
    message = format_result_notification('銘柄選定', 'スクリーニング', '完了しました。', ['採用銘柄数: 3件'])

    assert message.splitlines()[:5] == [
        '【業務】銘柄選定',
        '【機能】スクリーニング',
        '【概要】',
        '完了しました。',
        '【詳細】',
    ]