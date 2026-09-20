#!/usr/bin/env python3
"""Search common scholarly metadata services and optionally download OA PDFs."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


USER_AGENT = "research-literature/0.1 (Codex; contact via configured Unpaywall email)"


def fetch_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_bytes(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read(), response.headers.get("Content-Type", "")


def abstract_from_inverted(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in index.items():
        words.extend((position, word) for position in positions)
    return " ".join(word for _, word in sorted(words))


def clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def openalex(query: str, limit: int, email: str | None) -> list[dict[str, Any]]:
    params = {"search": query, "per-page": str(limit)}
    if email:
        params["mailto"] = email
    data = fetch_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
    rows = []
    for item in data.get("results", []):
        location = item.get("best_oa_location") or {}
        rows.append({
            "source": "OpenAlex",
            "title": clean(item.get("title")),
            "year": item.get("publication_year"),
            "doi": item.get("doi"),
            "url": item.get("id"),
            "oa": bool((item.get("open_access") or {}).get("is_oa")),
            "oa_url": location.get("pdf_url") or location.get("landing_page_url"),
            "authors": [a.get("author", {}).get("display_name") for a in item.get("authorships", [])],
            "abstract": abstract_from_inverted(item.get("abstract_inverted_index")),
        })
    return rows


def crossref(query: str, limit: int, email: str | None) -> list[dict[str, Any]]:
    params = {"query.bibliographic": query, "rows": str(limit)}
    if email:
        params["mailto"] = email
    data = fetch_json("https://api.crossref.org/works?" + urllib.parse.urlencode(params))
    rows = []
    for item in data.get("message", {}).get("items", []):
        date = item.get("published-print") or item.get("published-online") or {}
        parts = date.get("date-parts", [[]])[0]
        links = item.get("link", [])
        rows.append({
            "source": "Crossref",
            "title": clean((item.get("title") or [""])[0]),
            "year": parts[0] if parts else None,
            "doi": item.get("DOI"),
            "url": item.get("URL"),
            "oa": False,
            "oa_url": next((x.get("URL") for x in links if "pdf" in x.get("content-type", "").lower()), None),
            "authors": [clean((a.get("given", "") + " " + a.get("family", ""))) for a in item.get("author", [])],
            "abstract": clean(re.sub(r"<[^>]+>", " ", item.get("abstract", ""))),
        })
    return rows


def semantic_scholar(query: str, limit: int) -> list[dict[str, Any]]:
    fields = "title,abstract,year,authors,externalIds,openAccessPdf,url,venue"
    params = urllib.parse.urlencode({"query": query, "limit": str(min(limit, 100)), "fields": fields})
    data = fetch_json("https://api.semanticscholar.org/graph/v1/paper/search?" + params)
    rows = []
    for item in data.get("data", []):
        ids = item.get("externalIds") or {}
        oa = item.get("openAccessPdf") or {}
        rows.append({
            "source": "Semantic Scholar",
            "title": clean(item.get("title")),
            "year": item.get("year"),
            "doi": ids.get("DOI"),
            "url": item.get("url"),
            "oa": bool(oa.get("url")),
            "oa_url": oa.get("url"),
            "authors": [a.get("name") for a in item.get("authors", [])],
            "abstract": clean(item.get("abstract")),
        })
    return rows


def arxiv(query: str, limit: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"search_query": f"all:{query}", "start": "0", "max_results": str(limit)})
    request = urllib.request.Request("https://export.arxiv.org/api/query?" + params, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        root = ET.fromstring(response.read())
    ns = {"a": "http://www.w3.org/2005/Atom"}
    rows = []
    for item in root.findall("a:entry", ns):
        link = next((x.attrib.get("href") for x in item.findall("a:link", ns) if x.attrib.get("type") == "application/pdf"), None)
        rows.append({
            "source": "arXiv",
            "title": clean(item.findtext("a:title", default="", namespaces=ns)),
            "year": (item.findtext("a:published", default="", namespaces=ns) or "")[:4],
            "doi": None,
            "url": item.findtext("a:id", default="", namespaces=ns),
            "oa": True,
            "oa_url": link,
            "authors": [clean(x.findtext("a:name", default="", namespaces=ns)) for x in item.findall("a:author", ns)],
            "abstract": clean(item.findtext("a:summary", default="", namespaces=ns)),
        })
    return rows


def deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = (row.get("doi") or "").lower().replace("https://doi.org/", "")
        if not key:
            key = re.sub(r"[^a-z0-9]", "", row.get("title", "").lower())
        current = seen.get(key)
        if current is None or len(json.dumps(row, ensure_ascii=False)) > len(json.dumps(current, ensure_ascii=False)):
            seen[key] = row
    return list(seen.values())


def safe_name(title: str, year: Any) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._")[:100] or "paper"
    return f"{year or 'unknown'}_{stem}.pdf"


def download_open_access(rows: list[dict[str, Any]], target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for row in rows:
        url = row.get("oa_url")
        if not row.get("oa") or not url:
            continue
        try:
            content, content_type = fetch_bytes(url)
            if "pdf" not in content_type.lower() and not url.lower().split("?", 1)[0].endswith(".pdf"):
                continue
            path = target / safe_name(row.get("title", "paper"), row.get("year"))
            path.write_bytes(content)
            row["downloaded_to"] = str(path)
            time.sleep(0.5)
        except Exception as exc:
            row["download_error"] = str(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--email", help="Contact email for polite API access and Unpaywall-compatible workflows")
    parser.add_argument("--sources", default="openalex,crossref,semanticscholar,arxiv")
    parser.add_argument("--open-access-only", action="store_true")
    parser.add_argument("--download-dir", type=Path)
    parser.add_argument("--output", type=Path, help="Write JSON results to this path")
    args = parser.parse_args()
    rows: list[dict[str, Any]] = []
    sources = {x.strip().lower() for x in args.sources.split(",")}
    for name, fn in (("openalex", lambda: openalex(args.query, args.limit, args.email)),
                     ("crossref", lambda: crossref(args.query, args.limit, args.email)),
                     ("semanticscholar", lambda: semantic_scholar(args.query, args.limit)),
                     ("arxiv", lambda: arxiv(args.query, args.limit))):
        if name not in sources:
            continue
        try:
            rows.extend(fn())
        except Exception as exc:
            print(f"{name}: {exc}", file=sys.stderr)
    rows = deduplicate(rows)
    if args.open_access_only:
        rows = [row for row in rows if row.get("oa") and row.get("oa_url")]
    if args.download_dir:
        download_open_access(rows, args.download_dir)
    payload = {"query": args.query, "count": len(rows), "results": rows}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
