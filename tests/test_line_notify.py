from src.infrastructure.notification.line_notify import format_result_notification, wrap_for_line


def _display_width(text: str) -> int:
    return sum(2 if ord(character) > 127 else 1 for character in text)


def test_format_result_notification_uses_required_sections():
    message = format_result_notification('銘柄選定', 'スクリーニング', '完了しました。', ['採用銘柄数: 3件'])

    assert message.splitlines()[:5] == [
        '【業務】銘柄選定',
        '【機能】スクリーニング',
        '【概要】',
        '完了しました。',
        '【詳細】',
    ]


def test_wrap_for_line_limits_each_line_to_40_display_columns():
    message = wrap_for_line('20文字を超える日本語の通知本文はLINEで見やすい位置で改行します。')

    assert all(_display_width(line) <= 40 for line in message.splitlines())