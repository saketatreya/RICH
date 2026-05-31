"""Tests for pipeline_demo — integration-level, derived from root contract."""

from pipeline_demo import PipelineDemo


def test_pipeline_happy_path():
    demo = PipelineDemo(normalizer=None, validator=None)
    # We test the wiring logic by mocking deps inline
    class FakeNormalizer:
        def normalize(self, text):
            return {"normalized": text.strip().lower()}

    class FakeValidator:
        def validate(self, text):
            if not text:
                return {"valid": False, "reason": "empty"}
            return {"valid": True, "reason": "OK"}

    demo.normalizer = FakeNormalizer()
    demo.validator = FakeValidator()
    result = demo.run("  Hello World  ")
    assert result["original"] == "  Hello World  "
    assert result["normalized"] == "hello world"
    assert result["valid"] is True
