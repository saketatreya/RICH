def render(title: str, body: str, analysis: dict) -> dict:
    word_count = analysis.get('word_count', 0)
    reading_level = analysis.get('reading_level', '')
    summary = f'{title} ({word_count} words, {reading_level})'
    return {
        'published': {
            'title': title,
            'body': body,
            'analysis': analysis,
            'summary': summary
        }
    }
