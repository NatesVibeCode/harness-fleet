"""Strict input boundary for JSON, JSONL, CSV, TXT, PDF, and HTML records."""
from __future__ import annotations

import csv
import html as _html
import json
import re
import sys
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import InputItem

# A bundled dossier is one row, and a row can be far larger than csv's 128KB
# default field limit. Without this the export fails after the whole run is
# done, and a company's thorough evidence reads as a parse error.
csv.field_size_limit(min(2**31 - 1, sys.maxsize))


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._chunks: list[str] = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip and data.strip():
            self._chunks.append(data.strip())

    def get_text(self) -> str:
        return "\n\n".join(self._chunks)


def _strip_html(text: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(text)
        stripped = parser.get_text()
        # HTMLParser already decodes character references (convert_charrefs),
        # so only the regex fallback needs an explicit unescape.
        return stripped if stripped else _html.unescape(re.sub(r"<[^>]+>", " ", text))
    except Exception:
        return _html.unescape(re.sub(r"<[^>]+>", " ", text))


def _extract_pdf_text(path: Path) -> str:
    """Best-effort PDF extraction using pypdf if available; falls back to raw bytes decode."""
    import logging

    # See discover._extract_pdf_bytes: library chatter is not run output.
    for noisy in ("pypdf", "pdfminer", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = []
        for p in reader.pages:
            try:
                pages.append(p.extract_text() or "")
            except Exception:
                continue
        txt = "\n\n".join(pages).strip()
        if txt:
            return txt
    except Exception:
        pass
    # Fallback: try pdfminer.six
    try:
        from pdfminer.high_level import extract_text

        txt = extract_text(str(path)) or ""
        if txt.strip():
            return txt.strip()
    except Exception:
        pass
    # Last resort: decode bytes and hint user
    raw = path.read_bytes()
    # If PDF header present, warn
    try:
        decoded = raw.decode("utf-8", errors="ignore")
        # Strip PDF binary artifacts crudely
        decoded = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", decoded)
        if len(decoded.strip()) > 100 and looks_like_text(decoded):
            return decoded.strip()[:200_000]
    except Exception:
        pass
    raise ValueError(
        f"Cannot extract text from PDF '{path}': install 'pypdf' (pip install pypdf) or 'pdfminer.six' for robust extraction"
    )


class InputDataError(ValueError):
    pass


def looks_like_text(
    value: str,
    min_chars: int = 100,
    min_printable_ratio: float = 0.9,
    max_suspicious_ratio: float = 0.05,
) -> bool:
    """Heuristic gate against binary garbage decoded as text.

    Short strings are exempt (too noisy to judge). Longer ones must clear a
    printable-character ratio and stay under a suspicious-character ceiling
    (controls, formats, surrogates, private-use, decode replacements).
    Legitimate Latin-1/CJK prose passes; NUL/C1-laden decoder output does not.
    """
    import unicodedata

    if len(value) < min_chars:
        return True
    printable = sum(1 for ch in value if ch.isprintable() or ch in " \t\n\r\f\v")
    if (printable / len(value)) < min_printable_ratio:
        return False
    suspicious = sum(
        1 for ch in value
        if ch == "\ufffd" or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co")
    )
    return (suspicious / len(value)) <= max_suspicious_ratio


_JSON_CHUNK_SIZE = 64 * 1024


class _StreamingJSON:
    """Small standard-library JSON tokenizer that keeps one value at a time."""

    def __init__(self, handle: Any):
        self.handle = handle
        self.decoder = json.JSONDecoder()
        self.buffer = ""
        self.eof = False

    def _fill(self) -> bool:
        if self.eof:
            return False
        chunk = self.handle.read(_JSON_CHUNK_SIZE)
        if chunk == "":
            self.eof = True
            return False
        self.buffer += chunk
        return True

    def _skip_whitespace(self) -> None:
        while True:
            stripped = self.buffer.lstrip()
            if stripped:
                self.buffer = stripped
                return
            if not self._fill():
                return

    def peek(self) -> str:
        self._skip_whitespace()
        return self.buffer[:1]

    def consume(self, expected: str) -> None:
        actual = self.peek()
        if actual != expected:
            raise InputDataError(f"invalid JSON: expected '{expected}', found '{actual or 'end of input'}'")
        self.buffer = self.buffer[1:]

    def value(self, context: str) -> object:
        while True:
            self._skip_whitespace()
            if not self.buffer:
                raise InputDataError(f"invalid JSON: expected {context}, found end of input")
            try:
                value, end = self.decoder.raw_decode(self.buffer)
            except json.JSONDecodeError as exc:
                message = exc.msg
                incomplete = (
                    not self.eof
                    and (
                        exc.pos >= max(0, len(self.buffer) - 1)
                        or "Unterminated string" in message
                    )
                )
                if incomplete and self._fill():
                    continue
                raise InputDataError(f"invalid JSON: {message}") from exc
            self.buffer = self.buffer[end:]
            return value

    def finish(self) -> None:
        if self.peek():
            raise InputDataError("invalid JSON: trailing data after the input records")


def _iter_json_array(stream: _StreamingJSON) -> Iterator[object]:
    stream.consume("[")
    if stream.peek() == "]":
        stream.consume("]")
        return

    index = 0
    while True:
        yield stream.value(f"array item {index}")
        delimiter = stream.peek()
        if delimiter == "]":
            stream.consume("]")
            return
        if delimiter != ",":
            raise InputDataError("invalid JSON: expected ',' or ']' after an array item")
        stream.consume(",")
        if stream.peek() == "]":
            raise InputDataError("invalid JSON: trailing comma in input array")
        index += 1


def _iter_json_records(source: Path) -> Iterator[object]:
    """Yield records from a JSON array without loading the whole document."""
    with open(source, encoding="utf-8-sig", errors="replace") as handle:
        stream = _StreamingJSON(handle)
        first = stream.peek()
        if first == "[":
            yield from _iter_json_array(stream)
        elif first == "{":
            stream.consume("{")
            if stream.peek() == "}":
                stream.consume("}")
                raise InputDataError("input JSON object must contain an 'items' array")
            saw_items = False
            while True:
                key = stream.value("an object key")
                if not isinstance(key, str):
                    raise InputDataError("invalid JSON: object keys must be strings")
                stream.consume(":")
                if key != "items":
                    raise InputDataError("input JSON object must contain only an 'items' array")
                if saw_items:
                    raise InputDataError("input JSON object contains duplicate 'items' keys")
                saw_items = True
                yield from _iter_json_array(stream)
                delimiter = stream.peek()
                if delimiter == "}":
                    stream.consume("}")
                    break
                if delimiter != ",":
                    raise InputDataError("invalid JSON: expected ',' or '}' after the items array")
                stream.consume(",")
            if not saw_items:
                raise InputDataError("input JSON object must contain an 'items' array")
        else:
            raise InputDataError("input must be an array or an {items: [...]} object")
        stream.finish()


def _select_item(
    item: InputItem,
    target_ids: set[str] | None,
    fuzzy_ids: bool = False,
) -> InputItem | None:
    """Filter an item against target IDs; None target set keeps the item.

    Exact by default: IDs are content-addressed pipeline keys, so near-miss
    matching risks grounding the wrong record. Pass ``fuzzy_ids=True`` only
    to opt back into the legacy underscore/space equivalence.
    """
    if target_ids is None:
        return item
    if item.item_id in target_ids:
        return item
    if fuzzy_ids and (
        item.item_id.replace("_", " ") in target_ids
        or item.item_id.replace(" ", "_") in target_ids
    ):
        return item
    return None


def _validate_input_item(
    raw_item: object,
    index: int,
    seen: set[str],
    target_ids: set[str] | None,
    fuzzy_ids: bool = False,
) -> InputItem | None:
    try:
        item = InputItem.model_validate(raw_item)
    except ValidationError as exc:
        raise InputDataError(f"invalid item {index}: {exc.errors(include_url=False)}") from exc
    if not item.text.strip():
        raise InputDataError(f"invalid item {index}: text is blank")
    if item.item_id in seen:
        raise InputDataError(f"duplicate item_id: {item.item_id}")
    seen.add(item.item_id)
    return _select_item(item, target_ids, fuzzy_ids)


def _resolve_only_ids(only_ids: set[str] | list[str] | str | Path | None) -> set[str] | None:
    """Resolve an allowlist of item IDs from a set, comma-separated string, or file path."""
    if only_ids is None:
        return None
    if isinstance(only_ids, (set, list)):
        return {str(x).strip() for x in only_ids if str(x).strip()}

    candidate = None
    if isinstance(only_ids, Path):
        candidate = only_ids.expanduser()
    elif isinstance(only_ids, str) and not any(c in only_ids for c in ",;\n"):
        path = Path(only_ids).expanduser()
        try:
            if path.is_file() or path.suffix.lower() in {".csv", ".json", ".jsonl", ".txt"}:
                candidate = path
        except OSError:
            pass
    if candidate is not None and not candidate.is_file():
        raise InputDataError(f"ID filter file not found: {candidate}")

    if candidate and candidate.is_file():
        suffix = candidate.suffix.lower()
        if suffix == ".csv":
            with open(candidate, encoding="utf-8-sig", errors="replace") as f:
                reader = csv.DictReader(f)
                if reader.fieldnames:
                    id_col = None
                    for c in ("item_id", "id", "domain", "key", "name", "slug"):
                        if c in reader.fieldnames:
                            id_col = c
                            break
                    id_col = id_col or reader.fieldnames[0]
                    return {(row.get(id_col) or "").strip() for row in reader if (row.get(id_col) or "").strip()}
        elif suffix == ".jsonl":
            ids = set()
            for line in candidate.read_text(encoding="utf-8-sig").splitlines():
                if line.strip():
                    try:
                        d = json.loads(line)
                        if isinstance(d, dict):
                            val = d.get("item_id") or d.get("id")
                            if val:
                                ids.add(str(val).strip())
                    except Exception:
                        pass
            return ids
        elif suffix == ".json":
            try:
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
                records = data.get("records") if isinstance(data, dict) and "records" in data else (data if isinstance(data, list) else [])
                ids = set()
                for rec in records:  # type: ignore[union-attr]
                    if isinstance(rec, dict):
                        val = rec.get("item_id") or rec.get("id")
                        if val:
                            ids.add(str(val).strip())
                return ids
            except Exception:
                pass
        # Plain text: one ID per line
        return {line.strip() for line in candidate.read_text(encoding="utf-8-sig").splitlines() if line.strip()}

    if isinstance(only_ids, str):
        parts = [p.strip() for p in re.split(r"[,;\s]+", only_ids) if p.strip()]
        return set(parts) if parts else None

    return None


def iter_input_items(
    path: str | Path,
    id_column: str | None = None,
    text_column: str | None = None,
    title_column: str | None = None,
    uri_column: str | None = None,
    only_ids: set[str] | list[str] | str | Path | None = None,
    fuzzy_ids: bool = False,
) -> Iterator[InputItem]:
    """Yield validated input records while keeping memory bounded.

    JSON arrays, JSONL, and CSV are read incrementally.  ``load_input_items``
    remains available for callers that explicitly need a list.
    """
    source = Path(path)
    if not source.is_file():
        raise InputDataError(f"input file not found: {source}")
    suffix = source.suffix.lower()
    target_ids = _resolve_only_ids(only_ids)
    if suffix == ".csv" and target_ids is not None:
        target_ids = {value.replace(" ", "_").replace("/", "_").replace(":", "_") for value in target_ids}

    seen: set[str] = set()
    parsed_count = 0

    if suffix == ".jsonl":
        with open(source, encoding="utf-8-sig", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw_item = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise InputDataError(f"invalid JSONL at line {line_number}: {exc.msg}") from exc
                item = _validate_input_item(raw_item, parsed_count, seen, target_ids, fuzzy_ids)
                parsed_count += 1
                if item is not None:
                    yield item
    elif suffix == ".json":
        for raw_item in _iter_json_records(source):
            item = _validate_input_item(raw_item, parsed_count, seen, target_ids, fuzzy_ids)
            parsed_count += 1
            if item is not None:
                yield item
    elif suffix == ".csv":
        try:
            with open(source, encoding="utf-8-sig", errors="replace") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    raise InputDataError("CSV file has no header columns")
                fieldnames = list(reader.fieldnames)
                
                # Resolve ID column
                resolved_id_col = id_column
                if not resolved_id_col:
                    for candidate in ("item_id", "id", "domain", "key", "name", "slug"):
                        if candidate in fieldnames:
                            resolved_id_col = candidate
                            break
                    if not resolved_id_col:
                        resolved_id_col = fieldnames[0]
                elif resolved_id_col not in fieldnames:
                    raise InputDataError(f"Specified id column '{resolved_id_col}' not found in CSV columns: {fieldnames}")

                # Resolve text column
                resolved_text_col = text_column
                if not resolved_text_col:
                    for candidate in ("text", "research", "content", "body", "description", "summary", "input"):
                        if candidate in fieldnames:
                            resolved_text_col = candidate
                            break
                    if not resolved_text_col:
                        non_id = [col for col in fieldnames if col != resolved_id_col]
                        if non_id:
                            resolved_text_col = non_id[0]
                        else:
                            raise InputDataError(f"CSV requires a text column; found only '{fieldnames[0]}'")
                elif resolved_text_col not in fieldnames:
                    raise InputDataError(f"Specified text column '{resolved_text_col}' not found in CSV columns: {fieldnames}")

                if title_column and title_column not in fieldnames:
                    raise InputDataError(f"Specified title column '{title_column}' not found in CSV columns: {fieldnames}")
                resolved_title_col = title_column if title_column in fieldnames else ("title" if "title" in fieldnames else None)

                if uri_column and uri_column not in fieldnames:
                    raise InputDataError(f"Specified uri column '{uri_column}' not found in CSV columns: {fieldnames}")
                resolved_uri_col = uri_column if uri_column in fieldnames else ("source_uri" if "source_uri" in fieldnames else ("url" if "url" in fieldnames else None))

                for row_idx, row in enumerate(reader, start=1):
                    # Skip completely empty rows (common in spreadsheet exports / trailing newlines)
                    if not any((v or "").strip() for v in row.values() if v is not None):
                        continue

                    raw_id = (row.get(resolved_id_col) or "").strip()
                    if not raw_id:
                        raise InputDataError(f"CSV row {row_idx} has empty ID column '{resolved_id_col}'")
                    cleaned_id = raw_id.replace(" ", "_").replace("/", "_").replace(":", "_")
                    
                    raw_text = (row.get(resolved_text_col) or "").strip()
                    # Fail closed like JSONL: an ID with no evidence is a data
                    # bug, not an empty row (fully empty rows skip above).
                    if not raw_text:
                        raise InputDataError(f"CSV row {row_idx} has empty text column '{resolved_text_col}'")
                    
                    title = row.get(resolved_title_col) if resolved_title_col else None
                    uri = row.get(resolved_uri_col) if resolved_uri_col else None
                    
                    meta = {
                        k: v for k, v in row.items()
                        if k not in (resolved_id_col, resolved_text_col, resolved_title_col, resolved_uri_col)
                        and v is not None and v != ""
                    }
                    raw_item = {
                        "item_id": cleaned_id,
                        "text": raw_text,
                        "title": title or None,
                        "source_uri": uri or None,
                        "metadata": meta,
                    }
                    item = _validate_input_item(raw_item, row_idx - 1, seen, target_ids, fuzzy_ids)
                    parsed_count += 1
                    if item is not None:
                        yield item
        except Exception as exc:
            if isinstance(exc, InputDataError):
                raise
            raise InputDataError(f"failed to parse CSV: {exc}") from exc
    elif suffix in (".txt", ".md"):
        txt = source.read_text(encoding="utf-8", errors="replace")
        raw_item = {"item_id": source.stem.replace(" ", "_"), "text": txt, "title": source.name}
        item = _validate_input_item(raw_item, 0, seen, target_ids, fuzzy_ids)
        parsed_count = 1
        if item is not None:
            yield item
    elif suffix in (".html", ".htm"):
        html = source.read_text(encoding="utf-8", errors="replace")
        txt = _strip_html(html)
        if not txt.strip():
            raise InputDataError(f"HTML file '{source}' produced no extractable text")
        raw_item = {"item_id": source.stem.replace(" ", "_"), "text": txt, "title": source.name}
        item = _validate_input_item(raw_item, 0, seen, target_ids, fuzzy_ids)
        parsed_count = 1
        if item is not None:
            yield item
    elif suffix == ".pdf":
        txt = _extract_pdf_text(source)
        raw_item = {"item_id": source.stem.replace(" ", "_"), "text": txt, "title": source.name}
        item = _validate_input_item(raw_item, 0, seen, target_ids, fuzzy_ids)
        parsed_count = 1
        if item is not None:
            yield item
    else:
        raise InputDataError("input must use .json, .jsonl, .csv, .txt, .md, .html, or .pdf")

    if parsed_count == 0:
        raise InputDataError("input contains no items")


def load_input_items(
    path: str | Path,
    id_column: str | None = None,
    text_column: str | None = None,
    title_column: str | None = None,
    uri_column: str | None = None,
    only_ids: set[str] | list[str] | str | Path | None = None,
    fuzzy_ids: bool = False,
) -> list[InputItem]:
    """Compatibility wrapper that intentionally materializes a list."""
    return list(iter_input_items(
        path,
        id_column=id_column,
        text_column=text_column,
        title_column=title_column,
        uri_column=uri_column,
        only_ids=only_ids,
        fuzzy_ids=fuzzy_ids,
    ))
