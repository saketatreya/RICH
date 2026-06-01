def classify(body: str) -> dict:
    fragments = body.split('.')
    word_counts = [len(fragment.split()) for fragment in fragments]
    average = sum(word_counts) / len(word_counts) if word_counts else 0
    reading_level = 'easy' if average < 12 else 'hard'
    return {'reading_level': reading_level}
