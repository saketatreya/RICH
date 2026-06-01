class CommentIngestPipeline:
    def __init__(self, sanitize, moderate, enrich, format):
        self.sanitize = sanitize
        self.moderate = moderate
        self.enrich = enrich
        self.format = format

    def ingest(self, text: str) -> dict:
        s = self.sanitize.sanitize(text=text)
        m = self.moderate.moderate(text=s["text"])
        e = self.enrich.enrich(body=m["body"])
        r = self.format.format(
            body=m["body"],
            flagged=m["flagged"],
            word_count=e["word_count"],
            reading_time=e["reading_time"],
            preview=e["preview"],
        )
        return r
