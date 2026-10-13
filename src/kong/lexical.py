"""Shared, dependency-free FTS terms, extracted unchanged from continuity."""
import re


def lexical_terms(text: str) -> list[str]:
    # unicode61 does not segment Chinese words. Add overlapping CJK bigrams
    # explicitly, while retaining Latin words. No vector service required.
    words = re.findall(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+", text.lower())
    terms = []
    for word in words:
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            terms.extend(word[i:i + 2] for i in range(max(1, len(word) - 1)))
        else:
            terms.append(word)
    return list(dict.fromkeys(terms))
