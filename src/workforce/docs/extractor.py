"""Deterministic requirement extraction with stable identifiers."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from workforce.docs.parsers import ParsedDoc

EXPLICIT_ID = re.compile(r"^\s*(?:[-*]\s*)?\**\[?((?:REQ|FR|NFR|SEC|PERF|UI|API|DB|DATA|CON|AC|US)[-_ ]?\d+(?:\.\d+)*)\]?\**\s*[:.)\-–]\s*(.+)$", re.I)
MODAL = re.compile(r"\b(must|shall|should|is required to|are required to|will|needs? to|may not|must not|shall not|cannot)\b", re.I)
BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
GWT = re.compile(r"^\s*(?:[-*]\s*)?(given|when|then|and)\b", re.I)
AC_HEADING = re.compile(r"acceptance criteria|acceptance tests?|definition of done", re.I)
NON_REQ_HEADING = re.compile(r"\b(introduction|overview|background|glossary|references|revision history|out of scope|non-goals)\b", re.I)

CATEGORY_RULES = [
    ("security", re.compile(r"\b(auth\w*|password|encrypt\w*|token|permission|role-based|csrf|xss|sql injection|secret|hash\w*|tls|https)\b", re.I)),
    ("performance", re.compile(r"\b(latency|throughput|response time|within \d+\s*(ms|s|seconds)|per second|p9\d)\b", re.I)),
    ("data", re.compile(r"\b(database|schema|table|column|migration|persist\w*|stored?)\b", re.I)),
    ("api", re.compile(r"\b(endpoint|api|http|GET|POST|PUT|DELETE|PATCH|status code|json response)\b")),
    ("ui", re.compile(r"\b(screen|page|button|form|ui|display|click|dashboard)\b", re.I)),
]


@dataclass
class Candidate:
    key: str
    text: str
    section: str
    category: str = "functional"
    priority: str = "must"
    acceptance_criteria: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    verification_method: str = "test"
    explicit_id: bool = False

    @property
    def content_hash(self) -> str:
        return content_hash(self.text)


def normalize(text: str) -> str:
    """Strip Markdown emphasis/code markers but keep identifiers such as snake_case names intact."""
    text = re.sub(r"[*`]", "", text)
    text = re.sub(r"(?<![A-Za-z0-9])_([^_\n]+)_(?![A-Za-z0-9])", r"\1", text)  # _emphasis_ only
    return re.sub(r"\s+", " ", text).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).lower().encode()).hexdigest()


def categorize(text: str) -> str:
    for cat, pat in CATEGORY_RULES:
        if pat.search(text):
            return cat
    return "functional"


def priority_of(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(should|may)\b", t) and not re.search(r"\b(must|shall)\b", t):
        return "should"
    return "must"


def verification_of(category: str, text: str) -> str:
    if category == "performance":
        return "analysis"
    if re.search(r"\b(document(ed|ation)|readme|comment)\b", text, re.I):
        return "inspection"
    return "test"


def _stable_key(prefix: str, text: str) -> str:
    return f"REQ-{prefix}-{content_hash(text)[:6].upper()}"


def _doc_prefix(doc_stem: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", doc_stem.upper())[:8] or "DOC"


def extract(doc: ParsedDoc, doc_stem: str) -> list[Candidate]:
    prefix = _doc_prefix(doc_stem)
    out: list[Candidate] = []
    for item in doc.structured:
        text = normalize(item["text"])
        cat = item.get("category") or categorize(text)
        key = (item.get("id") or "").strip().upper().replace(" ", "-") or _stable_key(prefix, text)
        out.append(Candidate(key=key, text=text, section=item.get("section", ""), category=cat,
                             priority=item.get("priority") or priority_of(text),
                             acceptance_criteria=[normalize(a) for a in item.get("acceptance_criteria") or []],
                             dependencies=list(item.get("dependencies") or []),
                             verification_method=item.get("verification_method") or verification_of(cat, text),
                             explicit_id=bool(item.get("id"))))
    for sec in doc.sections:
        if NON_REQ_HEADING.search(sec.heading.split(" > ")[-1]):
            continue
        is_ac_section = bool(AC_HEADING.search(sec.heading.split(" > ")[-1]))
        current: Candidate | None = None
        for raw in sec.text.splitlines():
            line = raw.rstrip()
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            m_id = EXPLICIT_ID.match(line)
            if is_ac_section and out:
                # Acceptance criteria apply to the requirements of the parent section.
                parent = sec.heading.rsplit(" > ", 1)[0]
                targets = [c for c in out if c.section == parent] or [out[-1]]
                b = BULLET.match(line)
                for t in targets[-1:]:
                    t.acceptance_criteria.append(normalize(b.group(1) if b else line))
                continue
            if m_id:
                text = normalize(m_id.group(2))
                key = m_id.group(1).upper().replace(" ", "-").replace("_", "-")
                current = Candidate(key=key, text=text, section=sec.heading, category=categorize(text),
                                    priority=priority_of(text), verification_method=verification_of(categorize(text), text),
                                    explicit_id=True)
                out.append(current)
                continue
            b = BULLET.match(line)
            body = normalize(b.group(1) if b else line)
            if current is not None and indent >= 2 and b:
                # Nested bullet under a requirement => acceptance criterion
                current.acceptance_criteria.append(body)
                continue
            if GWT.match(line) and current is not None:
                current.acceptance_criteria.append(body)
                continue
            if MODAL.search(body) and len(body) > 12:
                for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])", body):
                    if MODAL.search(sentence) and len(sentence) > 12:
                        cat = categorize(sentence)
                        current = Candidate(key=_stable_key(prefix, sentence), text=sentence, section=sec.heading,
                                            category=cat, priority=priority_of(sentence),
                                            verification_method=verification_of(cat, sentence))
                        out.append(current)
            elif not b:
                current = None
    # Deduplicate by key (keep the first occurrence, merge acceptance criteria).
    dedup: dict[str, Candidate] = {}
    for c in out:
        if c.key in dedup:
            dedup[c.key].acceptance_criteria += [a for a in c.acceptance_criteria if a not in dedup[c.key].acceptance_criteria]
        else:
            dedup[c.key] = c
    for c in dedup.values():
        c.dependencies += [d.upper() for d in re.findall(r"\b(?:REQ|FR|NFR|SEC)-\d+\b", c.text, re.I)
                           if d.upper() != c.key and d.upper() not in c.dependencies]
    return list(dedup.values())
