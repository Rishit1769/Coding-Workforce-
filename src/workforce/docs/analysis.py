"""Ambiguity, gap and contradiction detection (deterministic heuristics).
The Architect agent performs an additional semantic review; nothing here is
ever silently resolved - every finding becomes an Issue for the human."""

from __future__ import annotations

import re
from itertools import combinations

from workforce.docs.extractor import Candidate

VAGUE = re.compile(
    r"\b(fast|quick(ly)?|user[- ]friendly|intuitive|easy|simple|robust|scalable|efficient|appropriate(ly)?|"
    r"as needed|if necessary|etc\.?|and so on|some|several|many|various|reasonable|adequate|flexible|"
    r"seamless(ly)?|modern|best practices?|state[- ]of[- ]the[- ]art|minimal|large|small|soon)\b",
    re.I)
PLACEHOLDER = re.compile(r"\b(TBD|TBA|TODO|FIXME|XXX)\b|\?\?\?")
NEGATION = re.compile(r"\b(must not|shall not|should not|may not|cannot|can not|never|no longer|is not allowed|disallow\w*|prohibit\w*)\b", re.I)
NUMBER_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*(characters?|chars?|ms|milliseconds?|seconds?|s|minutes?|items?|requests?|users?|MB|GB|KB|%|days?|hours?)\b", re.I)
STOP = set("the a an to of and or for in on with be is are must shall should will that this it as by from at user users system".split())


def _stem(w: str) -> str:
    for suf in ("ies", "s"):
        if w.endswith(suf) and len(w) > len(suf) + 3:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def _tokens(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 2}


def _content_tokens(text: str) -> set[str]:
    units = {"character", "char", "ms", "millisecond", "second", "minute", "item", "request", "day", "hour", "most",
             "least", "longer", "shorter", "than", "maximum", "minimum", "max", "min", "rejected", "allowed"}
    return {t for t in _tokens(text) if not t.isdigit() and t not in units}


def _statements(c: Candidate) -> list[str]:
    return [c.text, *c.acceptance_criteria]


def _unit(u: str) -> str:
    u = u.lower()
    for base in ("char", "ms", "millisecond", "second", "minute", "item", "request", "user", "day", "hour"):
        if u.startswith(base):
            return {"millisecond": "ms"}.get(base, base)
    return u


def detect_issues(cands: list[Candidate]) -> list[dict]:
    issues: list[dict] = []
    for c in cands:
        vague = sorted({m.group(0).lower() for m in VAGUE.finditer(c.text)} | {m.group(0) for m in PLACEHOLDER.finditer(c.text)})
        if vague:
            issues.append({"kind": "ambiguity", "severity": "major" if c.priority == "must" else "minor",
                           "requirement_keys": [c.key],
                           "description": f"{c.key} uses unmeasurable/vague terms {vague}: \"{c.text}\". "
                                          "Provide measurable criteria."})
        if not c.acceptance_criteria and c.verification_method == "test" and c.priority == "must" and len(c.text) < 400:
            issues.append({"kind": "gap", "severity": "minor", "requirement_keys": [c.key],
                           "description": f"{c.key} has no explicit acceptance criteria; tests will be derived from the "
                                          "requirement text only."})
    keys = {c.key for c in cands}
    for c in cands:
        for d in c.dependencies:
            if d not in keys:
                issues.append({"kind": "gap", "severity": "major", "requirement_keys": [c.key],
                               "description": f"{c.key} references {d}, which is not defined in any document."})
    for a, b in combinations(cands, 2):
        ta, tb = _tokens(a.text), _tokens(b.text)
        if not ta or not tb:
            continue
        overlap = len(ta & tb) / len(ta | tb)
        if overlap >= 0.45 and bool(NEGATION.search(a.text)) != bool(NEGATION.search(b.text)):
            issues.append({"kind": "contradiction", "severity": "major", "requirement_keys": [a.key, b.key],
                           "description": f"{a.key} and {b.key} appear to state opposite rules: "
                                          f"\"{a.text}\" vs \"{b.text}\"."})
            continue
        found = None
        for sa in _statements(a):
            na = {_unit(u): float(n) for n, u in NUMBER_UNIT.findall(sa)}
            if not na:
                continue
            for sb in _statements(b):
                nb = {_unit(u): float(n) for n, u in NUMBER_UNIT.findall(sb)}
                clash = [u for u in na.keys() & nb.keys() if na[u] != nb[u]]
                if clash and _content_tokens(sa) & _content_tokens(sb):
                    found = (clash, sa, sb)
                    break
            if found:
                break
        if found:
            clash, sa, sb = found
            issues.append({"kind": "contradiction", "severity": "major", "requirement_keys": [a.key, b.key],
                           "description": f"{a.key} and {b.key} specify different values for {clash}: "
                                          f"\"{sa}\" vs \"{sb}\"."})
    return issues
