"""Email checker: compose format_checker and domain_checker over shared regex_engine."""


class EmailChecker:
    def __init__(self, regex_engine, format_checker, domain_checker):
        self.regex_engine = regex_engine
        self.format_checker = format_checker
        self.domain_checker = domain_checker

    def check(self, email: str) -> dict:
        """Check email format and domain. Both checkers share the same regex engine."""
        fmt = self.format_checker.check_format(email)
        dom = self.domain_checker.is_common(email)
        return {
            "email": email,
            "valid_format": fmt["valid"],
            "common_domain": dom["common"],
            "domain": dom["domain"],
        }
