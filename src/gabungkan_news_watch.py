"""Gabungkan keluaran CSV Newswatch dan buat tabel artikel serta validasi."""

import argparse
import csv
import hashlib
import json
import logging
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = ROOT / "data" / "raw"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "interim" / "consolidation"

STANDARD_COLUMNS = (
    "title",
    "publish_date",
    "author",
    "content",
    "keyword",
    "category",
    "source",
    "link",
    "hazard_type",
)
METADATA_COLUMNS = (
    "query_group",
    "sourcefile",
    "scraped_at",
    "url_normalized",
    "article_id",
    "content_hash",
    "content_raw",
    "content_clean",
    "duplicate_of",
)
COLUMN_ALIASES = {
    "date": "publish_date",
    "published_at": "publish_date",
    "publication_date": "publish_date",
    "url": "link",
    "query": "keyword",
    "keywords": "keyword",
    "media": "source",
    "scrape_timestamp": "scraped_at",
    "scrape_time": "scraped_at",
}
TRACKING_PARAMETERS = {
    "dclid",
    "fbclid",
    "gclid",
    "gclsrc",
    "mc_cid",
    "mc_eid",
    "msclkid",
    "ref_src",
    "yclid",
}
HTML_TAG_PATTERN = re.compile(r"<[^>]*>")
WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_url(value):
    """Canonicalize URL formatting and remove tracking-only query parameters."""
    raw_url = (value or "").strip()
    if not raw_url:
        return ""
    try:
        parts = urlsplit(raw_url)
        hostname = parts.hostname
        if parts.scheme.lower() not in {"http", "https"} or not hostname:
            return ""
        if parts.username is not None or parts.password is not None:
            return ""

        scheme = parts.scheme.lower()
        hostname = hostname.encode("idna").decode("ascii").lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = parts.port
        netloc = hostname
        if port is not None and not (
            (scheme == "http" and port == 80)
            or (scheme == "https" and port == 443)
        ):
            netloc = f"{netloc}:{port}"

        path = parts.path or "/"
        if path != "/":
            path = path.rstrip("/") or "/"
        query_pairs = []
        for key, item_value in parse_qsl(parts.query, keep_blank_values=True):
            lowered_key = key.lower()
            if lowered_key.startswith("utm_") or lowered_key in TRACKING_PARAMETERS:
                continue
            query_pairs.append((key, item_value))
        query_pairs.sort()
        query = urlencode(query_pairs, doseq=True)
        return urlunsplit((scheme, netloc, path, query, ""))
    except (UnicodeError, ValueError):
        return ""


def normalize_content(value):
    """Lowercase text, remove punctuation, and collapse whitespace for hashing."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = HTML_TAG_PATTERN.sub(" ", text)
    text = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in text
    )
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def generate_content_hash(content):
    """Return a SHA-256 digest, or an empty string when content is absent."""
    normalized = normalize_content(content)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def generate_article_id(url_normalized):
    """Return a stable shortened SHA-1 ID for a normalized URL."""
    if not url_normalized:
        return ""
    digest = hashlib.sha1(url_normalized.encode("utf-8")).hexdigest()
    return digest[:20]


def infer_query_group(path):
    """Use the batch filename as the query group, merging its optional `2` copy."""
    group = re.sub(r"^geohazard_\d+_", "", path.stem, flags=re.IGNORECASE)
    if group.endswith("2"):
        companion = path.with_name(path.stem[:-1] + path.suffix)
        if companion.exists():
            group = group[:-1]
    return group or path.stem


def _canonicalize_row(raw_row, headers):
    raw_values = {
        str(header).strip(): value for header, value in raw_row.items() if header
    }
    row = {
        header: (raw_values.get(header) or "").strip() for header in headers
    }
    normalized_headers = {
        header.strip().casefold(): header for header in headers if header
    }

    for canonical in STANDARD_COLUMNS:
        source_header = normalized_headers.get(canonical)
        if source_header and source_header != canonical and not row.get(canonical):
            row[canonical] = row.get(source_header, "")

    for alias, canonical in COLUMN_ALIASES.items():
        alias_header = normalized_headers.get(alias)
        canonical_header = normalized_headers.get(canonical)
        if alias_header and alias_header != canonical_header:
            if not row.get(canonical):
                row[canonical] = row.get(alias_header, "")
        elif alias_header and canonical_header is None:
            row[canonical] = row.get(alias_header, "")

    scraped_at = normalized_headers.get("scraped_at")
    scrape_timestamp = normalized_headers.get("scrape_timestamp")
    if not row.get("scraped_at"):
        row["scraped_at"] = row.get(scraped_at or "", "") or row.get(
            scrape_timestamp or "", ""
        )

    for column in STANDARD_COLUMNS:
        row.setdefault(column, "")
    return row


def load_datasets(input_dir):
    """Load top-level source CSVs and add normalized metadata to every row."""
    input_dir = Path(input_dir)
    files = sorted(input_dir.glob("*.csv"))
    if not files:
        raise FileNotFoundError(f"Tidak ada CSV sumber di {input_dir}")

    records = []
    source_counts = {}
    source_columns = []
    column_mappings = {}

    for path in files:
        file_records = []
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                raise ValueError(f"CSV tidak memiliki header: {path.name}")
            headers = [header.strip() for header in reader.fieldnames]
            normalized_headers = {
                header.casefold(): header for header in headers if header
            }
            applied_mappings = {}
            for canonical in STANDARD_COLUMNS:
                source_header = normalized_headers.get(canonical)
                if source_header and source_header != canonical:
                    applied_mappings[source_header] = canonical
            for alias, canonical in COLUMN_ALIASES.items():
                alias_header = normalized_headers.get(alias)
                canonical_header = normalized_headers.get(canonical)
                if alias_header and alias_header != canonical_header:
                    applied_mappings[alias_header] = canonical
            if applied_mappings:
                column_mappings[path.name] = applied_mappings

            query_group = infer_query_group(path)
            for raw_row in reader:
                row = _canonicalize_row(raw_row, headers)
                normalized_url = normalize_url(row.get("link", ""))
                row["query_group"] = query_group
                row["sourcefile"] = path.name
                row["scraped_at"] = row.get("scraped_at", "")
                row["url_normalized"] = normalized_url
                row["article_id"] = generate_article_id(normalized_url)
                row["content_hash"] = generate_content_hash(row.get("content", ""))
                row["content_raw"] = row.get("content", "")
                row["content_clean"] = ""
                row["duplicate_of"] = ""
                file_records.append(row)

            for header in headers:
                if header not in source_columns:
                    source_columns.append(header)

        records.extend(file_records)
        source_counts[path.name] = len(file_records)
        logging.info("Memuat %s baris dari %s", len(file_records), path.name)

    return records, source_columns, source_counts, column_mappings


def _best_record_key(record_with_index):
    index, row = record_with_index
    return (
        len(row.get("content", "")),
        sum(bool(row.get(column, "").strip()) for column in STANDARD_COLUMNS),
        -index,
    )


def aggregate_articles(records):
    """Aggregate URL-identical rows, keeping the most complete content record."""
    grouped = defaultdict(list)
    unlinked = []
    for index, row in enumerate(records):
        article_id = row.get("article_id", "")
        if article_id:
            grouped[article_id].append((index, row))
        else:
            unlinked.append((index, row))

    articles = []
    query_map = []
    for article_id, group in grouped.items():
        primary = max(group, key=_best_record_key)[1]
        article = dict(primary)
        query_groups = sorted({row["query_group"] for _, row in group})
        source_files = sorted({row["sourcefile"] for _, row in group})
        if not article.get("scraped_at"):
            article["scraped_at"] = next(
                (row["scraped_at"] for _, row in group if row.get("scraped_at")),
                "",
            )
        article["query_group"] = json.dumps(query_groups, ensure_ascii=False)
        article["sourcefile"] = json.dumps(source_files, ensure_ascii=False)
        article["content_raw"] = article.get("content", "")
        article["content_clean"] = ""
        article["duplicate_of"] = ""
        articles.append(article)
        query_map.extend(
            {"article_id": article_id, "query_group": query_group}
            for query_group in query_groups
        )

    for _, row in unlinked:
        article = dict(row)
        article["content_raw"] = article.get("content", "")
        article["content_clean"] = ""
        article["duplicate_of"] = ""
        articles.append(article)

    articles.sort(key=lambda row: (row.get("article_id", ""), row.get("sourcefile", "")))
    query_map.sort(key=lambda row: (row["article_id"], row["query_group"]))
    return articles, query_map


def find_content_duplicate_groups(articles):
    """Find articles with identical normalized content but different URLs."""
    grouped = defaultdict(list)
    for article in articles:
        content_hash = article.get("content_hash", "")
        if content_hash and article.get("url_normalized"):
            grouped[content_hash].append(article)

    duplicate_groups = []
    for content_hash, group in sorted(grouped.items()):
        urls = sorted({article["url_normalized"] for article in group})
        if len(urls) < 2:
            continue
        duplicate_groups.append(
            {
                "content_hash": content_hash,
                "article_count": len({article["article_id"] for article in group}),
                "article_ids": json.dumps(
                    sorted({article["article_id"] for article in group}),
                    ensure_ascii=False,
                ),
                "urls": json.dumps(urls, ensure_ascii=False),
                "query_groups": json.dumps(
                    sorted(
                        {
                            query_group
                            for article in group
                            for query_group in json.loads(article["query_group"])
                        }
                    ),
                    ensure_ascii=False,
                ),
            }
        )
    return duplicate_groups


def apply_content_duplicate_labels(articles, duplicate_groups):
    """Mark all but one deterministic representative in each content duplicate group."""
    articles_by_id = {
        article["article_id"]: article
        for article in articles
        if article.get("article_id")
    }
    labeled_count = 0
    for group in duplicate_groups:
        article_ids = json.loads(group["article_ids"])
        if len(article_ids) < 2:
            continue
        canonical_id = min(article_ids)
        for article_id in sorted(article_ids):
            if article_id == canonical_id:
                continue
            article = articles_by_id.get(article_id)
            if article is not None:
                article["duplicate_of"] = canonical_id
                labeled_count += 1
    return labeled_count


def _write_csv(path, rows, fieldnames):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def consolidate(input_dir, output_dir):
    records, source_columns, source_counts, column_mappings = load_datasets(input_dir)
    articles, query_map = aggregate_articles(records)
    content_duplicate_groups = find_content_duplicate_groups(articles)
    labeled_duplicate_count = apply_content_duplicate_labels(
        articles, content_duplicate_groups
    )

    extra_columns = [
        column
        for column in source_columns
        if column not in STANDARD_COLUMNS and column not in METADATA_COLUMNS
    ]
    long_fields = list(STANDARD_COLUMNS) + extra_columns + list(METADATA_COLUMNS)
    article_fields = long_fields
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "newswatch_long.csv", records, long_fields)
    _write_csv(output_dir / "articles.csv", articles, article_fields)
    _write_csv(
        output_dir / "article_query_map.csv",
        query_map,
        ("article_id", "query_group"),
    )
    _write_csv(
        output_dir / "content_duplicate_groups.csv",
        content_duplicate_groups,
        ("content_hash", "article_count", "article_ids", "urls", "query_groups"),
    )

    article_ids = [article["article_id"] for article in articles if article["article_id"]]
    duplicate_id_count = len(article_ids) - len(set(article_ids))
    rows_per_article = defaultdict(int)
    for row in records:
        if row["article_id"]:
            rows_per_article[row["article_id"]] += 1
    multi_query_count = sum(
        len(json.loads(article["query_group"])) > 1
        for article in articles
        if article.get("article_id")
    )
    rows_before = sum(source_counts.values())
    report = {
        "input_files": len(source_counts),
        "source_rows": source_counts,
        "rows_before_concat": rows_before,
        "rows_after_concat": len(records),
        "concat_row_count_matches_sources": rows_before == len(records),
        "rows_after_article_aggregation": len(articles),
        "rows_collapsed_by_article_id": len(records) - len(articles),
        "articles_with_multiple_source_rows": sum(
            count > 1 for count in rows_per_article.values()
        ),
        "source_rows_in_multirow_articles": sum(
            count for count in rows_per_article.values() if count > 1
        ),
        "unique_article_ids": len(set(article_ids)),
        "duplicate_article_id_count": duplicate_id_count,
        "missing_or_invalid_url_count": sum(
            not row["url_normalized"] for row in records
        ),
        "rows_with_scraped_at": sum(bool(row["scraped_at"]) for row in records),
        "articles_in_multiple_query_groups": multi_query_count,
        "content_duplicate_groups_with_different_urls": len(content_duplicate_groups),
        "articles_in_different_url_content_groups": sum(
            group["article_count"] for group in content_duplicate_groups
        ),
        "cross_url_duplicate_labels": labeled_duplicate_count,
        "column_mappings_applied": column_mappings,
        "validation_passed": (
            rows_before == len(records)
            and len(records) >= len(articles)
            and duplicate_id_count == 0
        ),
    }
    with (output_dir / "validation_report.json").open(
        "w", encoding="utf-8"
    ) as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Gabungkan CSV Newswatch dan agregasikan artikel berdasarkan URL."
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    report = consolidate(args.input_dir, args.output_dir)
    print(f"Baris sebelum concat: {report['rows_before_concat']}")
    print(f"Baris setelah concat: {report['rows_after_concat']}")
    print(f"Artikel setelah agregasi: {report['rows_after_article_aggregation']}")
    print(f"Baris teragregasi: {report['rows_collapsed_by_article_id']}")
    print(f"ID artikel ganda: {report['duplicate_article_id_count']}")
    print(
        "Artikel di lebih dari satu kueri: "
        f"{report['articles_in_multiple_query_groups']}"
    )
    print(
        "Grup konten sama dengan URL berbeda: "
        f"{report['content_duplicate_groups_with_different_urls']}"
    )
    print(
        "Artikel lintas-URL diberi label duplicate_of: "
        f"{report['cross_url_duplicate_labels']}"
    )
    print(f"Laporan validasi: {args.output_dir / 'validation_report.json'}")
    return 0 if report["validation_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
