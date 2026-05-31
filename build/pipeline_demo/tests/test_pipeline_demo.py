"""Tests for pipeline_demo — integration-level."""

from pipeline_demo import PipelineDemo


class FakeNormalizer:
    def normalize(self, text):
        return {"normalized": text.strip().lower()}


class FakeValidator:
    def validate(self, text):
        if not text:
            return {"valid": False, "reason": "empty"}
        return {"valid": True, "reason": "OK"}


def test_pipeline_happy_path():
    demo = PipelineDemo(FakeNormalizer(), FakeValidator())
    result = demo.run("  Hello World  ")
    assert result["original"] == "  Hello World  "
    assert result["normalized"] == "hello world"
    assert result["valid"] is True
