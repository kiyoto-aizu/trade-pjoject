"""現在の環境変数(.env)で起動時の設定値検証を実行する運用者向けスクリプト。

使い方: python scripts/check_startup_config.py (venvのpythonを使う)
違反があれば全件表示して終了コード1。表示は項目名・値・許容範囲のみ(秘密値は出さない)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import config  # noqa: E402
from src.config.startup_validation import find_config_violations  # noqa: E402


def main() -> int:
    violations = find_config_violations(config)
    if not violations:
        print("OK: 起動時の設定値検証に違反はありません。")
        return 0
    print(f"NG: 設定値の違反が{len(violations)}件あります。")
    for violation in violations:
        print(f"- {violation.format()}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
