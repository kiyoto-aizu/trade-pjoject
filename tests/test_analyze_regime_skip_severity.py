from scripts.analysis.analyze_regime_skip_severity import classify_atr_severity


def test_atr_severity_borderline_boundaries():
    threshold = 2.0

    assert classify_atr_severity(threshold - 0.001, threshold) == "閾値未満"
    assert classify_atr_severity(threshold, threshold) == "ボーダーライン"
    assert classify_atr_severity(threshold * 1.1 - 0.001, threshold) == "ボーダーライン"
    assert classify_atr_severity(threshold * 1.1, threshold) == "中間域"


def test_atr_severity_clear_danger_boundary():
    threshold = 2.0

    assert classify_atr_severity(threshold * 1.5 - 0.001, threshold) == "中間域"
    assert classify_atr_severity(threshold * 1.5, threshold) == "明確な危険域"