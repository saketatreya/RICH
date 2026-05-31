# M-G: Depth-2 canned test data

CANNED_DEEP_DECISION = {
    "is_leaf": False,
    "children": [
        {
            "id": "username_checker",
            "description": "Check username is 3-20 chars, alphanumeric only",
            "interface": {
                "operations": [
                    {"name": "check", "inputs": {"username": "string"},
                     "outputs": {"valid": "bool", "reason": "string"}, "errors": []}
                ]
            },
            "dependencies": [],
            "behavior": [{"id": "length", "prose": "Username must be 3-20 characters"}],
        },
        {
            "id": "password_pipeline",
            "description": "Check password: min length AND has digit+letter",
            "interface": {
                "operations": [
                    {"name": "check", "inputs": {"password": "string"},
                     "outputs": {"valid": "bool", "reason": "string"}, "errors": []}
                ]
            },
            "dependencies": [],
            "behavior": [
                {"id": "length", "prose": "Password must be at least 8 chars"},
                {"id": "complex", "prose": "Password must contain both a digit and a letter"},
            ],
        },
        {
            "id": "token_generator",
            "description": "Generate a simple welcome token from username",
            "interface": {
                "operations": [
                    {"name": "generate", "inputs": {"username": "string"},
                     "outputs": {"token": "string"}, "errors": []}
                ]
            },
            "dependencies": [],
            "behavior": [{"id": "unique", "prose": "Token is username + random suffix"}],
        },
    ],
    "edges": [
        {"from": "username_checker", "to": "password_pipeline", "name": "username_result"},
        {"from": "password_pipeline", "to": "token_generator", "name": "password_result"},
    ],
}

# password_pipeline's own decomposition (depth 2)
CANNED_PASSWORD_PIPELINE_DECISION = {
    "is_leaf": False,
    "children": [
        {
            "id": "length_check",
            "description": "Check password has at least 8 characters",
            "interface": {
                "operations": [
                    {"name": "check", "inputs": {"password": "string"},
                     "outputs": {"valid": "bool", "reason": "string"}, "errors": []}
                ]
            },
            "dependencies": [],
            "behavior": [{"id": "min8", "prose": "Password must be >= 8 characters"}],
        },
        {
            "id": "complexity_check",
            "description": "Check password contains at least one digit and one letter",
            "interface": {
                "operations": [
                    {"name": "check", "inputs": {"password": "string"},
                     "outputs": {"valid": "bool", "reason": "string"}, "errors": []}
                ]
            },
            "dependencies": [],
            "behavior": [{"id": "digit_letter", "prose": "Must have at least one digit and one letter"}],
        },
    ],
    "edges": [
        {"from": "length_check", "to": "complexity_check", "name": "length_result"},
    ],
}

CANNED_IMPLS_DEEP = {
    "username_checker": '''def check(username: str) -> dict:
    if len(username) < 3:
        return {"valid": False, "reason": "Too short (min 3)"}
    if len(username) > 20:
        return {"valid": False, "reason": "Too long (max 20)"}
    if not username.isalnum():
        return {"valid": False, "reason": "Must be alphanumeric"}
    return {"valid": True, "reason": "OK"}
''',
    "length_check": '''def check(password: str) -> dict:
    if len(password) < 8:
        return {"valid": False, "reason": "Too short (min 8)"}
    return {"valid": True, "reason": "OK"}
''',
    "complexity_check": '''def check(password: str) -> dict:
    has_digit = any(c.isdigit() for c in password)
    has_letter = any(c.isalpha() for c in password)
    if not has_digit:
        return {"valid": False, "reason": "No digit found"}
    if not has_letter:
        return {"valid": False, "reason": "No letter found"}
    return {"valid": True, "reason": "OK"}
''',
    "password_pipeline": '''class PasswordPipeline:
    def __init__(self, length_check, complexity_check):
        self.length_check = length_check
        self.complexity_check = complexity_check
    def check(self, password: str) -> dict:
        r1 = self.length_check.check(password)
        if not r1["valid"]:
            return r1
        r2 = self.complexity_check.check(password)
        return r2
''',
    "token_generator": '''import hashlib, time
def generate(username: str) -> dict:
    raw = f"{username}-{time.time()}"
    token = hashlib.md5(raw.encode()).hexdigest()[:8]
    return {"token": f"welcome_{token}"}
''',
    "validate_registration": '''class ValidateRegistration:
    def __init__(self, username_checker, password_pipeline, token_generator):
        self.username_checker = username_checker
        self.password_pipeline = password_pipeline
        self.token_generator = token_generator
    def validate(self, username: str, password: str) -> dict:
        u = self.username_checker.check(username)
        if not u["valid"]:
            return {"username_ok": False, "password_ok": False, "token": "", "reason": u["reason"]}
        p = self.password_pipeline.check(password)
        if not p["valid"]:
            return {"username_ok": True, "password_ok": False, "token": "", "reason": p["reason"]}
        t = self.token_generator.generate(username)
        return {"username_ok": True, "password_ok": True, "token": t["token"], "reason": "OK"}
''',
}

CANNED_TESTS_DEEP = {
    "username_checker": '''from username_checker import check
def test_valid():
    assert check("alice") == {"valid": True, "reason": "OK"}
def test_too_short():
    assert check("ab")["valid"] is False
def test_too_long():
    assert check("a"*25)["valid"] is False
def test_special():
    assert check("a@b")["valid"] is False
''',
    "length_check": '''from length_check import check
def test_valid():
    assert check("password123") == {"valid": True, "reason": "OK"}
def test_too_short():
    assert check("abc")["valid"] is False
''',
    "complexity_check": '''from complexity_check import check
def test_valid():
    assert check("abc123") == {"valid": True, "reason": "OK"}
def test_no_digit():
    assert check("abcdef")["valid"] is False
def test_no_letter():
    assert check("123456")["valid"] is False
''',
    "password_pipeline": '''from password_pipeline import PasswordPipeline
class FakeLength: pass
class FakeComplexity: pass
fl = FakeLength(); fl.check = lambda p: {"valid": True, "reason": "OK"}
fc = FakeComplexity(); fc.check = lambda p: {"valid": True, "reason": "OK"}
def test_pass():
    r = PasswordPipeline(fl, fc).check("x")
    assert r == {"valid": True, "reason": "OK"}
''',
    "token_generator": '''from token_generator import generate
def test_generates():
    r = generate("alice")
    assert r["token"].startswith("welcome_")
''',
    "validate_registration": '''from validate_registration import ValidateRegistration
class FakeU: pass
class FakeP: pass
class FakeT: pass
fu = FakeU(); fu.check = lambda u: {"valid": True, "reason": "OK"}
fp = FakeP(); fp.check = lambda p: {"valid": True, "reason": "OK"}
ft = FakeT(); ft.generate = lambda u: {"token": "welcome_xxx"}
def test_valid():
    r = ValidateRegistration(fu, fp, ft).validate("alice", "pass1234")
    assert r["username_ok"] and r["password_ok"]
    assert r["token"] == "welcome_xxx"
''',
}