"""Generate the environment-variable reference without importing config.py."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "src" / "config" / "config.py"
OUTPUT_PATH = ROOT / "docs" / "reference" / "config-reference.md"
GROUPS = (
    ("ATR", "ATR"),
    ("レジーム", "REGIME"),
    ("スクリーニング", "SCREENING"),
    ("フィルタ", "FILTER"),
    ("paper", "PAPER"),
    ("Slack", "SLACK"),
    ("LLM", "LLM"),
    ("ログ", "LOG"),
    ("その他", ""),
)
SECRET_NAME = re.compile(r"PASSWORD|WEBHOOK|API_KEY|TOKEN", re.IGNORECASE)


def _string_values(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _string_values(node.body) + _string_values(node.orelse)
    return []


def _preceding_comments(lines: list[str], line_number: int) -> str:
    comments: list[str] = []
    index = line_number - 2
    while index >= 0:
        stripped = lines[index].strip()
        if not stripped.startswith("#"):
            break
        comments.append(stripped[1:].strip())
        index -= 1
    comments.reverse()
    return " ".join(comment for comment in comments if comment)


def _environment_calls(statement: ast.stmt):
    for node in ast.walk(statement):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "os"
            and function.attr == "getenv"
            and node.args
        ):
            for name in _string_values(node.args[0]):
                default = node.args[1] if len(node.args) > 1 else None
                yield name, default, False
        elif (
            isinstance(function, ast.Name)
            and function.id == "_non_negative_env"
            and len(node.args) >= 2
        ):
            for name in _string_values(node.args[0]):
                yield name, node.args[1], False
        elif isinstance(function, ast.Name) and function.id == "_load_required_env" and node.args:
            for name in _string_values(node.args[0]):
                yield name, None, True


def _group_for(name: str) -> str:
    upper_name = name.upper()
    for title, prefix in GROUPS[:-1]:
        if prefix in upper_name:
            return title
    return "その他"


def _expression(
    node: ast.AST | None,
    variable_defaults: dict[str, ast.AST],
    resolving: frozenset[str] = frozenset(),
) -> str:
    if node is None:
        return "(未指定)"
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return json.dumps(node.value, ensure_ascii=False)
        if node.value is None:
            return "None"
        return str(node.value)
    if isinstance(node, ast.Name) and node.id in variable_defaults and node.id not in resolving:
        return _expression(
            variable_defaults[node.id], variable_defaults, resolving | {node.id}
        )
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
        and node.func.attr == "getenv"
        and node.args
    ):
        fallback = (
            _expression(node.args[1], variable_defaults, resolving)
            if len(node.args) > 1 else "None"
        )
        return f"{_expression(node.args[0], variable_defaults, resolving)} (未設定時 {fallback})"
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "str"
        and node.args
    ):
        argument = node.args[0]
        if isinstance(argument, ast.Name) and argument.id in variable_defaults:
            return _expression(variable_defaults[argument.id], variable_defaults, resolving)
    return ast.unparse(node)


def _collect_config(source: str) -> tuple[dict[str, dict[str, object]], dict[str, ast.AST]]:
    tree = ast.parse(source, filename=str(CONFIG_PATH))
    lines = source.splitlines()
    variable_defaults: dict[str, ast.AST] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = [target.id for target in statement.targets if isinstance(target, ast.Name)]
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            targets = [statement.target.id]
        else:
            continue
        for name, default, required in _environment_calls(statement):
            if not required and default is not None:
                for target in targets:
                    variable_defaults[target] = default
    entries: dict[str, dict[str, object]] = {}
    for statement in tree.body:
        description = _preceding_comments(lines, statement.lineno)
        for name, default, required in _environment_calls(statement):
            entry = entries.setdefault(name, {
                "default": default,
                "required": required,
                "description": description,
            })
            if required:
                entry["required"] = True
                entry["default"] = None
            elif entry["default"] is None and default is not None:
                entry["default"] = default
            if not entry["description"] and description:
                entry["description"] = description
    return entries, variable_defaults


def _render(
    entries: dict[str, dict[str, object]],
    variable_defaults: dict[str, ast.AST],
) -> tuple[str, int]:
    lines = [
        "# 環境変数リファレンス",
        "",
        "`src/config/config.py`をimportせずAST解析して生成。既定値はソース上の環境変数フォールバック式です。",
        "秘密情報に該当する名前は値を表示しません。説明は設定宣言直前のコメントのみを転記しています。",
        "",
    ]
    empty_descriptions = 0
    for title, _ in GROUPS:
        grouped = [
            (name, entry) for name, entry in sorted(entries.items())
            if _group_for(name) == title
        ]
        if not grouped:
            continue
        lines.extend([
            f"## {title}",
            "",
            "| 環境変数名 | 既定値 | 必須か | 説明（直前コメント） |",
            "|---|---|---|---|",
        ])
        for name, entry in grouped:
            default = (
                "(非表示)" if SECRET_NAME.search(name)
                else _expression(entry["default"], variable_defaults)
            )
            required = "必須" if entry["required"] else "任意"
            description = str(entry["description"] or "")
            if not description:
                empty_descriptions += 1
            description = description.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| `{name}` | {default} | {required} | {description} |")
        lines.append("")
    lines.extend([
        f"説明欄が空の項目: {empty_descriptions}件。",
        "",
    ])
    return "\n".join(lines), empty_descriptions


def main() -> None:
    source = CONFIG_PATH.read_text(encoding="utf-8")
    entries, variable_defaults = _collect_config(source)
    document, empty_descriptions = _render(entries, variable_defaults)
    OUTPUT_PATH.write_text(document, encoding="utf-8")
    print(f"環境変数: {len(entries)}件")
    print(f"説明欄が空: {empty_descriptions}件")
    print(f"出力: {OUTPUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()