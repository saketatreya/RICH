"""Format checker: validate email format using injected regex engine."""


class FormatChecker:
    def __init__(self, regex):
        self.regex = regex

    def check_format(self, email: str) -> dict:
        """Check email has valid format: contains @, has domain part."""
        result = self.regex.matches(email, r"^[^@]+@[^@]+\.[^@]+$")
        return {"valid": result["ok"]}
