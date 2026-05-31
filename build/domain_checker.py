"""Domain checker: check if email domain is common using injected regex engine."""


class DomainChecker:
    def __init__(self, regex):
        self.regex = regex

    def is_common(self, email: str) -> dict:
        """Check if email domain is gmail/yahoo/outlook."""
        result = self.regex.matches(email, r"@(gmail|yahoo|outlook)\.")
        if result["ok"]:
            return {"common": True, "domain": result["match"].lstrip("@").rstrip(".")}
        return {"common": False, "domain": ""}
