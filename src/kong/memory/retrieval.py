"""Small deterministic query planner; all candidates must already be authorized."""
import re

from kong.lexical import lexical_terms


GENERIC = frozenset({"what", "which", "when", "where", "why", "how", "is", "are", "the", "a", "an",
    "our", "we", "i", "you", "it", "of", "to", "and", "or", "in", "on", "with", "about", "that", "this",
    "its", "was", "did", "do", "does", "please", "recall", "remember", "let", "lets", "let's", "focus",
    "project", "episode", "incident", "experience", "badge", "recovery", "outage", "migration", "launch",
    "milestone", "verification", "final", "finally", "last", "time", "result", "end", "understood", "synthetic",
    "discuss", "talk", "tell", "show", "explain", "return", "continue", "acknowledge", "before", "after", "for",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"})


def is_reference(text):
    return bool(re.search(r"那次|那回|当时|后来|最后|\b(?:that|it|its|then|last time|in the end)\b", text, re.I))


def entities(text):
    # Proper names and explicit project labels are strong; generic retrieval words
    # alone can never manufacture a project match. No private entity catalog scan.
    values = re.findall(r"\b[A-Z][A-Za-z0-9_-]{1,60}\b", text)
    values += re.findall(r"\b([a-zA-Z][\w-]{1,60})\s+(?:project|outage|recovery|migration|launch|milestone)\b", text, re.I)
    values += re.findall(r"\bproject\s+([a-zA-Z][\w-]{1,60})\b", text, re.I)
    values += re.findall(r"[“\"「]([\u3400-\u9fffA-Za-z0-9_-]{2,40})[”\"」]\s*项目", text)
    values += re.findall(r"(?:项目(?:[：:\s]+[“\"「]?|[“\"「]))([\u3400-\u9fffA-Za-z0-9_-]{2,16})", text)
    values += re.findall(r"([\u3400-\u9fffA-Za-z0-9_-]{2,12})项目", text)
    return tuple(dict.fromkeys(v.lower() for v in values if v.lower() not in GENERIC))[:4]


def time_clues(text):
    dates = tuple(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text))
    months = tuple(f"{index:02d}" for index, name in enumerate(
        ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)
        if re.search(r"\b" + name + r"\b", text, re.I))
    return dates, months


def constrain(records, query, strong):
    """Do not choose a tied Episode just because its DB identifier sorts first."""
    if query.episode_ids:
        records = [r for r in records if r.kind == "episodic" and r.record_id in query.episode_ids]
        return records, "available" if records else "no_usable_information", "exact_episode"
    reference = is_reference(query.text) and not entities(query.text)
    if reference:
        episodes = [r for r in records if r.kind == "episodic"]
        if episodes:
            records = episodes
        dates, months = time_clues(query.text + " " + query.focus)
        if dates or months:
            def matches(record):
                episode = record.current.episode
                when = episode.when if episode else record.occurred_at
                evidence = record.current.content + (" " + when.isoformat() if when else "")
                evidence_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", evidence)
                return (any(date in evidence_dates for date in dates) if dates else
                        any(date[5:7] in months for date in evidence_dates))
            records = [r for r in records if matches(r)]
        if len(strong) > 1 or len(records) > 1:
            return [], "ambiguous", "ambiguous_focus"
        if not strong and not dates and not months:
            return [], "ambiguous", "no_anchor"
    return records, "available" if records else "no_usable_information", "entity_focus" if strong else "lexical"


def search_terms(query):
    direct = entities(query.text)
    reference = is_reference(query.text)
    strong = direct or (entities(query.focus) if reference else ())
    if not strong and reference and query.project_id:
        strong = (query.project_id.lower(),)
    text = query.text + (" " + query.focus if reference and not direct else "")
    terms = [t for t in lexical_terms(text) if t not in GENERIC][:64]
    return strong, terms
