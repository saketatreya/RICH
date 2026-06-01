def check(body: str) -> dict:
    return {"has_links": "http" in body}
