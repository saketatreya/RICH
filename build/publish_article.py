class PublishArticlePipeline:
    def __init__(self, parse_article, analyze_body, render_article):
        self.parse_article = parse_article
        self.analyze_body = analyze_body
        self.render_article = render_article

    def publish(self, raw: str) -> dict:
        parsed = self.parse_article.parse(raw=raw)
        title = parsed["title"]
        body = parsed["body"]

        analyzed = self.analyze_body.analyze(body=body)
        analysis = analyzed["analysis"]

        rendered = self.render_article.render(title=title, body=body, analysis=analysis)
        published = rendered["published"]

        return {
            "title": title,
            "body": body,
            "analysis": analysis,
            "published": published,
        }
