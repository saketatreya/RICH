def assemble(word_count: int, reading_level: str, top_keyword: str, has_links: bool) -> dict:
    return {
        "analysis": {
            "word_count": word_count,
            "reading_level": reading_level,
            "top_keyword": top_keyword,
            "has_links": has_links,
        }
    }
