def find_top(body: str) -> dict:
    tokens = body.split()
    normalized = [t.lower() for t in tokens]
    counts = {}
    for token in normalized:
        counts[token] = counts.get(token, 0) + 1
    if not counts:
        return {"top_keyword": ""}
    top = min(counts, key=lambda w: (-counts[w], w))
    return {"top_keyword": top}
