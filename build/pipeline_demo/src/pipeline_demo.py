"""Pipeline demo: normalize → validate.

Receives normalizer + validator as injected dependencies.
"""


class PipelineDemo:
    def __init__(self, normalizer, validator):
        self.normalizer = normalizer
        self.validator = validator

    def run(self, text: str) -> dict:
        """Run the pipeline: normalize then validate."""
        norm_result = self.normalizer.normalize(text)
        val_result = self.validator.validate(norm_result["normalized"])
        return {
            "original": text,
            "normalized": norm_result["normalized"],
            "valid": val_result["valid"],
            "reason": val_result["reason"],
        }
