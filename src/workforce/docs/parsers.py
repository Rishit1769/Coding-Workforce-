"""Document parsers. Each returns a list of Sections (heading path + text) and
optionally pre-structured requirement candidates (for JSON/YAML/OpenAPI)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SUPPORTED = {".md": "markdown", ".markdown": "markdown", ".txt": "text", ".pdf": "pdf", ".docx": "docx",
             ".json": "json", ".yaml": "yaml", ".yml": "yaml"}


@dataclass
class Section:
    heading: str
    text: str
    level: int = 1


@dataclass
class ParsedDoc:
    kind: str
    sections: list[Section]
    structured: list[dict] = field(default_factory=list)  # pre-extracted requirement candidates
    raw_text: str = ""


def detect_kind(path: Path) -> str:
    kind = SUPPORTED.get(path.suffix.lower())
    if kind is None:
        raise ValueError(f"unsupported document type: {path.suffix}")
    if kind in ("json", "yaml"):
        try:
            data = _load_structured(path, kind)
            if isinstance(data, dict) and ("openapi" in data or "swagger" in data):
                return "openapi"
        except Exception:
            pass
    return kind


def _load_structured(path: Path, kind: str):
    txt = path.read_text()
    return json.loads(txt) if kind == "json" else yaml.safe_load(txt)


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def sections_from_markdown(text: str) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []

    def flush():
        if buf and "\n".join(buf).strip():
            sections.append(Section(" > ".join(h for _, h in stack) or "(preamble)", "\n".join(buf).strip(),
                                    stack[-1][0] if stack else 0))
        buf.clear()

    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
        m = _HEADING.match(line) if not in_code else None
        if m:
            flush()
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2).strip()))
        else:
            buf.append(line)
    flush()
    return sections


def sections_from_plaintext(text: str) -> list[Section]:
    """Treat ALL-CAPS lines, numbered headings ("1. Title", "2.3 Title") and lines ending in ':' as headings."""
    md_lines = []
    for line in text.splitlines():
        s = line.strip()
        if s and len(s) < 80 and (s.isupper() and len(s) > 3 or re.match(r"^\d+(\.\d+)*\.?\s+[A-Z][^.]*$", s)
                                  and not re.search(r"\b(must|shall|should)\b", s, re.I)):
            depth = s.split()[0].count(".") + 1 if s[0].isdigit() else 1
            md_lines.append("#" * min(depth + 1, 6) + " " + s)
        else:
            md_lines.append(line)
    return sections_from_markdown("\n".join(md_lines))


def parse_pdf(path: Path) -> ParsedDoc:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return ParsedDoc("pdf", sections_from_plaintext(text), raw_text=text)


def parse_docx(path: Path) -> ParsedDoc:
    import docx

    d = docx.Document(str(path))
    lines = []
    for p in d.paragraphs:
        style = (p.style.name or "").lower() if p.style is not None else ""
        if style.startswith("heading"):
            n = re.findall(r"\d+", style)
            lines.append("#" * (int(n[0]) if n else 1) + " " + p.text)
        elif "list" in style:
            lines.append("- " + p.text)
        else:
            lines.append(p.text)
    for table in d.tables:
        for row in table.rows:
            lines.append("- " + " | ".join(c.text.strip() for c in row.cells))
    text = "\n".join(lines)
    return ParsedDoc("docx", sections_from_markdown(text), raw_text=text)


def parse_openapi(path: Path, kind: str) -> ParsedDoc:
    spec = _load_structured(path, "json" if path.suffix == ".json" else "yaml")
    structured: list[dict] = []
    for p, ops in (spec.get("paths") or {}).items():
        for method, op in (ops or {}).items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            op = op or {}
            responses = op.get("responses") or {}
            ac = [f"{method.upper()} {p} returns {code} {('- ' + (r or {}).get('description', '')) if r else ''}".strip()
                  for code, r in responses.items()]
            params = [x.get("name") for x in op.get("parameters", []) if isinstance(x, dict) and x.get("required")]
            if params:
                ac.append(f"required parameters: {', '.join(params)}")
            if op.get("requestBody", {}).get("required"):
                ac.append("request body is required and validated against its schema")
            structured.append({
                "id": op.get("operationId") and f"API-{op['operationId']}",
                "section": f"paths > {p} > {method.upper()}",
                "text": f"The API must implement {method.upper()} {p}: {op.get('summary') or op.get('description') or ''}".strip(),
                "category": "api", "acceptance_criteria": ac, "verification_method": "test",
            })
    for name, schema in ((spec.get("components") or {}).get("schemas") or {}).items():
        req = schema.get("required") or []
        props = schema.get("properties") or {}
        ac = [f"field '{f}' is required" for f in req] + [
            f"field '{k}' has type {v.get('type', 'object')}" + (f" (max length {v['maxLength']})" if 'maxLength' in v else "")
            + (f" (enum {v['enum']})" if "enum" in v else "") for k, v in props.items() if isinstance(v, dict)]
        structured.append({"id": f"API-SCHEMA-{name}", "section": f"components > schemas > {name}",
                           "text": f"The API data contract must define schema {name}.", "category": "data",
                           "acceptance_criteria": ac, "verification_method": "test"})
    return ParsedDoc("openapi", [], structured=structured, raw_text=path.read_text())


def parse_structured(path: Path, kind: str) -> ParsedDoc:
    data = _load_structured(path, kind)
    structured: list[dict] = []
    items = data.get("requirements") if isinstance(data, dict) else data if isinstance(data, list) else None
    if isinstance(items, list):
        for it in items:
            if isinstance(it, str):
                structured.append({"text": it})
            elif isinstance(it, dict) and (it.get("text") or it.get("description")):
                structured.append({
                    "id": it.get("id"), "text": it.get("text") or it.get("description"),
                    "section": it.get("section", ""), "category": it.get("category") or it.get("type"),
                    "priority": it.get("priority"), "acceptance_criteria": it.get("acceptance_criteria") or [],
                    "dependencies": it.get("dependencies") or it.get("depends_on") or [],
                    "verification_method": it.get("verification") or it.get("verification_method"),
                })
        return ParsedDoc(kind, [], structured=structured, raw_text=path.read_text())
    # Generic config/schema documents: flatten into "constraint" text sections.
    flat = yaml.safe_dump(data, sort_keys=False)
    return ParsedDoc(kind, [Section(path.name, flat)], raw_text=flat)


def parse_document(path: Path) -> ParsedDoc:
    kind = detect_kind(path)
    if kind == "markdown":
        t = path.read_text()
        return ParsedDoc(kind, sections_from_markdown(t), raw_text=t)
    if kind == "text":
        t = path.read_text()
        return ParsedDoc(kind, sections_from_plaintext(t), raw_text=t)
    if kind == "pdf":
        return parse_pdf(path)
    if kind == "docx":
        return parse_docx(path)
    if kind == "openapi":
        return parse_openapi(path, kind)
    return parse_structured(path, kind)
