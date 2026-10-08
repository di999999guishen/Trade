from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

from .config import resolve_project_path
from .store import connect, upsert_event


def _text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def _fingerprint(title: str) -> str:
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", title.lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _published_at(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _fetch(source: dict) -> tuple[bytes, str]:
    request = urllib.request.Request(source["url"], headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": urllib.parse.urljoin(source["url"], "/"),
    })
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(), response.headers.get_content_charset() or "utf-8"


def _rss_items(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    items = []
    for node in root.findall(".//item"):
        items.append({
            "title": _text(node.findtext("title")),
            "summary": _text(node.findtext("description")),
            "url": _text(node.findtext("link")),
            "published_at": _text(node.findtext("pubDate")) or None,
        })
    if not items:
        atom = "{http://www.w3.org/2005/Atom}"
        for node in root.findall(f".//{atom}entry"):
            link = node.find(f"{atom}link")
            items.append({"title": _text(node.findtext(f"{atom}title")), "summary": _text(node.findtext(f"{atom}summary")), "url": link.get("href", "") if link is not None else "", "published_at": _text(node.findtext(f"{atom}updated")) or None})
    return [item for item in items if item["title"]]


class _NDRCParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.current_href = None
        self.current_text: list[str] = []
        self.items: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current_href = dict(attrs).get("href")
            self.current_text = []

    def handle_data(self, data):
        if self.current_href is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current_href is not None:
            title = _text("".join(self.current_text))
            if len(title) >= 8 and self.current_href not in {"#", "/"}:
                self.items.append({"title": title, "summary": "", "url": urllib.parse.urljoin(self.base_url, self.current_href), "published_at": None})
            self.current_href = None


def _ndrc_items(raw: bytes, charset: str, base_url: str) -> list[dict]:
    parser = _NDRCParser(base_url)
    parser.feed(raw.decode(charset, errors="replace"))
    unique = {item["url"]: item for item in parser.items}
    return list(unique.values())[:100]


def _score(item: dict, source: dict, cfg: dict) -> dict:
    combined = (item["title"] + " " + item.get("summary", "")).lower()
    exposures = []
    hits = 0
    for exposure, keywords in cfg["news"]["exposure_keywords"].items():
        matched = sum(int(keyword.lower() in combined) for keyword in keywords)
        if matched:
            exposures.append(exposure)
            hits += matched
    if not exposures:
        exposures = list(source.get("default_exposures", []))
        relevance = float(source.get("default_relevance", 0.25 if exposures else 0.05))
    else:
        relevance = min(1.0, 0.45 + 0.12 * hits)
    reliability = float(source["reliability"])
    directness = float(source["directness"])
    evidence = 0.45 * reliability + 0.35 * relevance + 0.20 * directness
    return {
        "canonical_hash": _fingerprint(item["title"]), "title": item["title"], "summary": item.get("summary", ""),
        "event_type": "commodity_or_macro_news", "reliability_score": reliability,
        "relevance_score": relevance, "directness_score": directness, "evidence_score": round(evidence, 4),
        "exposures": exposures, "evidence_status": "source_fact_unreviewed_impact",
    }


def fetch_news(cfg: dict) -> dict:
    raw_dir = resolve_project_path(cfg, cfg["news"]["raw_dir"])
    raw_dir.mkdir(parents=True, exist_ok=True)
    db_path = resolve_project_path(cfg, cfg["state_db"])
    report = {"sources": {}, "inserted_events": 0, "seen_events": 0}
    with connect(db_path) as connection:
        for source in cfg["news"]["sources"]:
            fetched_at = datetime.now(timezone.utc).isoformat()
            try:
                raw, charset = _fetch(source)
                sha = hashlib.sha256(raw).hexdigest()
                extension = "xml" if source["type"] == "rss" else "html"
                path = raw_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{source['id']}.{extension}"
                path.write_bytes(raw)
                items = _rss_items(raw) if source["type"] == "rss" else _ndrc_items(raw, charset, source["url"])
                inserted = 0
                for item in items:
                    event = _score(item, source, cfg)
                    _, created = upsert_event(connection, event, {
                        "source_id": source["id"], "source_url": source["url"], "article_url": item.get("url", ""),
                        "published_at": _published_at(item.get("published_at")), "fetched_at_utc": fetched_at,
                        "raw_snapshot_path": str(path.resolve()), "raw_snapshot_sha256": sha,
                    })
                    inserted += int(created)
                report["sources"][source["id"]] = {"status": "ok", "items": len(items), "inserted": inserted, "snapshot": str(path.resolve()), "sha256": sha}
                report["inserted_events"] += inserted
                report["seen_events"] += len(items)
            except Exception as exc:
                report["sources"][source["id"]] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    return report


def recent_events(cfg: dict, limit: int = 20, relevant_only: bool = True) -> list[dict]:
    db_path = resolve_project_path(cfg, cfg["state_db"])
    with connect(db_path) as connection:
        where = "WHERE exposures_json <> '[]'" if relevant_only else ""
        rows = connection.execute(
            """SELECT event_id,title,event_type,evidence_score,exposures_json,evidence_status,first_seen_at_utc,last_seen_at_utc
            FROM events """ + where + " ORDER BY first_seen_at_utc DESC LIMIT ?", (limit,),
        ).fetchall()
    return [{"event_id": row[0], "title": row[1], "event_type": row[2], "evidence_score": row[3], "exposures": json.loads(row[4]), "evidence_status": row[5], "first_seen_at_utc": row[6], "last_seen_at_utc": row[7]} for row in rows]
