from src.infrastructure.analysis.error_analyzer import OpenAIErrorAnalyzer


def _raise_value_error(message="boom"):
    raise ValueError(message)


def _make_llm_stub(monkeypatch, content="推定原因\n- テスト用の原因です。"):
    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    def post(url, **kwargs):
        captured["payload"] = kwargs["json"]
        return DummyResponse()

    monkeypatch.setattr("src.infrastructure.analysis.error_analyzer.requests.post", post)
    return captured


def test_error_analyzer_calls_llm_on_first_occurrence(monkeypatch, tmp_path):
    captured = _make_llm_stub(monkeypatch)
    monkeypatch.setattr("src.infrastructure.analysis.error_analyzer._STATE_FILE", tmp_path / "state.json")

    analyzer = OpenAIErrorAnalyzer("key", "model", "https://example.test", cooldown_minutes=60)

    try:
        _raise_value_error()
    except ValueError as exc:
        result = analyzer.analyze_exception("テスト処理", exc)

    assert result.summary.startswith("推定原因")
    assert result.occurrence_count == 1
    assert result.is_new is True
    assert "ValueError" in captured["payload"]["messages"][1]["content"]


def test_error_analyzer_reuses_cached_result_and_counts_occurrences(monkeypatch, tmp_path):
    calls = []

    def post(url, **kwargs):
        calls.append(url)

        class DummyResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "推定原因\n- 1回目"}}]}

        return DummyResponse()

    monkeypatch.setattr("src.infrastructure.analysis.error_analyzer.requests.post", post)
    monkeypatch.setattr("src.infrastructure.analysis.error_analyzer._STATE_FILE", tmp_path / "state.json")

    analyzer = OpenAIErrorAnalyzer("key", "model", "https://example.test", cooldown_minutes=60)

    try:
        _raise_value_error(message="code=7203 price=1500")
    except ValueError as exc:
        first_result = analyzer.analyze_exception("テスト処理", exc)

    try:
        _raise_value_error(message="code=9984 price=2200")
    except ValueError as exc:
        second_result = analyzer.analyze_exception("テスト処理", exc)

    assert first_result.occurrence_count == 1
    assert second_result.occurrence_count == 2
    assert second_result.summary == first_result.summary
    assert second_result.is_new is False
    assert len(calls) == 1


def test_error_analyzer_re_analyzes_after_cooldown_expires(monkeypatch, tmp_path):
    calls = []

    def post(url, **kwargs):
        calls.append(url)

        class DummyResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": f"推定原因\n- {len(calls)}回目"}}]}

        return DummyResponse()

    monkeypatch.setattr("src.infrastructure.analysis.error_analyzer.requests.post", post)
    monkeypatch.setattr("src.infrastructure.analysis.error_analyzer._STATE_FILE", tmp_path / "state.json")

    analyzer = OpenAIErrorAnalyzer("key", "model", "https://example.test", cooldown_minutes=0)

    try:
        _raise_value_error()
    except ValueError as exc:
        analyzer.analyze_exception("テスト処理", exc)

    try:
        _raise_value_error()
    except ValueError as exc:
        result = analyzer.analyze_exception("テスト処理", exc)

    assert len(calls) == 2
    assert result.occurrence_count == 2


def test_error_analyzer_skips_minor_retryable_exceptions(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "src.infrastructure.analysis.error_analyzer.requests.post",
        lambda url, **kwargs: calls.append(url),
    )
    monkeypatch.setattr("src.infrastructure.analysis.error_analyzer._STATE_FILE", tmp_path / "state.json")

    analyzer = OpenAIErrorAnalyzer("key", "model", "https://example.test", cooldown_minutes=60)

    try:
        raise ConnectionError("接続できません")
    except ConnectionError as exc:
        result = analyzer.analyze_exception("テスト処理", exc)

    assert result is None
    assert calls == []

