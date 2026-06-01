def analyze(body: str) -> dict:
    words = body.split()
    word_count = len(words)

    sentences = body.split('.')
    non_empty = [s for s in sentences if s.strip()]
    if non_empty:
        avg_wps = sum(len(s.split()) for s in non_empty) / len(non_empty)
    else:
        avg_wps = 0
    reading_level = 'easy' if avg_wps < 12 else 'hard'

    freq = {}
    for w in words:
        lw = w.lower()
        freq[lw] = freq.get(lw, 0) + 1
    top_keyword = min(freq, key=lambda k: (-freq[k], k)) if freq else ''

    has_links = 'http' in body

    return {
        'analysis': {
            'word_count': word_count,
            'reading_level': reading_level,
            'top_keyword': top_keyword,
            'has_links': has_links,
        }
    }
