"""Preprocess and quality-check the consolidated Newswatch CSV."""

import argparse
import csv
import hashlib
import html
import json
import logging
import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = (
    ROOT
    / "data"
    / "interim"
    / "consolidation"
    / "articles.csv"
)
DEFAULT_OUTPUT_DIR = (
    ROOT / "data" / "interim" / "quality_control"
)
HTML_TAG_PATTERN = re.compile(r"<[^>]*>")
WHITESPACE_PATTERN = re.compile(r"\s+")
FAIL_FLAGS = {
    "empty_title",
    "empty_content",
    "missing_publish_date",
    "invalid_publish_date",
    "publish_date_before_min_year",
    "future_publish_date",
    "invalid_url",
    "article_id_mismatch",
    "content_hash_mismatch",
}
REVIEW_FLAGS = {
    "title_too_short",
    "content_too_short",
    "content_too_long",
    "missing_source",
    "missing_query_group",
}


def clean_text(value):
    """Unescape HTML, remove tags, and normalize whitespace without changing case."""
    text = html.unescape(value or "")
    text = HTML_TAG_PATTERN.sub(" ", text)
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def normalize_for_hash(value):
    """Apply the same lightweight text normalization used by phase 1."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = HTML_TAG_PATTERN.sub(" ", text)
    text = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in text
    )
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def expected_content_hash(value):
    normalized = normalize_for_hash(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def expected_article_id(url_normalized):
    if not url_normalized:
        return ""
    return hashlib.sha1(url_normalized.encode("utf-8")).hexdigest()[:20]


def parse_publish_date(value):
    """Parse common ISO and day/month/year publication date formats."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass

    formats = (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d %B %Y",
    )
    for date_format in formats:
        try:
            return datetime.strptime(text, date_format)
        except ValueError:
            continue
    return None


def is_valid_http_url(value):
    try:
        parts = urlsplit((value or "").strip())
        return parts.scheme.lower() in {"http", "https"} and bool(parts.hostname)
    except ValueError:
        return False


def _percent(count, total):
    return round((100 * count / total), 2) if total else 0.0


def _length_profile(values):
    if not values:
        return {"min": 0, "median": 0, "p95": 0, "max": 0}
    ordered = sorted(values)
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "min": ordered[0],
        "median": median(ordered),
        "p95": ordered[p95_index],
        "max": ordered[-1],
    }


def _clean_row(raw_row, input_fields, min_content_chars, max_content_chars, min_year, as_of_date, future_tolerance_days):
    row = {field: raw_row.get(field, "") or "" for field in input_fields}
    title_cleaned = clean_text(row.get("title", ""))
    content_cleaned = clean_text(row.get("content", ""))
    title_length = len(title_cleaned)
    content_length = len(content_cleaned)
    content_words = len(re.findall(r"\b\w+\b", content_cleaned, flags=re.UNICODE))
    parsed_date = parse_publish_date(row.get("publish_date", ""))
    url_normalized = (row.get("url_normalized") or "").strip()
    flags = []

    if not title_cleaned:
        flags.append("empty_title")
    elif title_length < 10:
        flags.append("title_too_short")

    if not content_cleaned:
        flags.append("empty_content")
    elif content_length < min_content_chars:
        flags.append("content_too_short")
    elif content_length > max_content_chars:
        flags.append("content_too_long")

    if not (row.get("publish_date") or "").strip():
        flags.append("missing_publish_date")
    elif parsed_date is None:
        flags.append("invalid_publish_date")
    else:
        if parsed_date.year < min_year:
            flags.append("publish_date_before_min_year")
        if parsed_date.date() > as_of_date + timedelta(days=future_tolerance_days):
            flags.append("future_publish_date")

    if not is_valid_http_url(row.get("link", "")) or not is_valid_http_url(
        url_normalized
    ):
        flags.append("invalid_url")
    elif expected_article_id(url_normalized) != (row.get("article_id") or "").strip():
        flags.append("article_id_mismatch")

    actual_content_hash = (row.get("content_hash") or "").strip()
    if actual_content_hash != expected_content_hash(row.get("content", "")):
        flags.append("content_hash_mismatch")

    if not (row.get("source") or "").strip():
        flags.append("missing_source")
    if not (row.get("query_group") or "").strip():
        flags.append("missing_query_group")

    row["title_cleaned"] = title_cleaned
    row["content_cleaned"] = content_cleaned
    if parsed_date is None:
        normalized_date = ""
    elif "T" in row.get("publish_date", "") or " " in row.get(
        "publish_date", ""
    ):
        normalized_date = parsed_date.isoformat(sep=" ")
    else:
        normalized_date = parsed_date.date().isoformat()
    row["publish_date_normalized"] = normalized_date
    row["title_length"] = title_length
    row["content_length"] = content_length
    row["content_word_count"] = content_words
    row["date_parse_ok"] = str(parsed_date is not None).lower()
    row["url_valid"] = str(
        is_valid_http_url(row.get("link", ""))
        and is_valid_http_url(url_normalized)
    ).lower()
    row["qc_flags"] = ";".join(flags)
    row["qc_status"] = (
        "fail"
        if any(flag in FAIL_FLAGS for flag in flags)
        else "review"
        if any(flag in REVIEW_FLAGS for flag in flags)
        else "pass"
    )
    return row, flags, parsed_date


def _read_csv(path):
    rows = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"CSV tidak memiliki header: {path}")
        fields = [field.strip() for field in reader.fieldnames]
        for raw_row in reader:
            rows.append(
                {
                    field: (raw_row.get(original_field) or "")
                    for field, original_field in zip(fields, reader.fieldnames)
                }
            )
    return fields, rows


def _write_csv(path, rows, fields):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def run_qc(
    input_path,
    output_dir,
    min_content_chars=100,
    max_content_chars=100000,
    min_year=1900,
    as_of_date=None,
    future_tolerance_days=0,
):
    input_path = Path(input_path)
    if not input_path.is_file():
        raise FileNotFoundError(f"File input tidak ditemukan: {input_path}")
    if min_content_chars < 1:
        raise ValueError("--min-content-chars harus lebih besar dari 0")
    if max_content_chars < min_content_chars:
        raise ValueError("--max-content-chars harus >= --min-content-chars")
    if future_tolerance_days < 0:
        raise ValueError("--future-tolerance-days tidak boleh negatif")
    if min_year < 1:
        raise ValueError("--min-year harus lebih besar dari 0")

    as_of_date = as_of_date or date.today()
    input_fields, raw_rows = _read_csv(input_path)
    processed_rows = []
    flag_counts = Counter()
    missing_counts = Counter()
    query_counts = Counter()
    source_counts = Counter()
    article_rows = defaultdict(list)
    content_rows = defaultdict(list)
    content_urls = defaultdict(set)
    parsed_dates = []
    title_lengths = []
    content_lengths = []
    content_word_counts = []

    for index, raw_row in enumerate(raw_rows):
        row, flags, parsed_date = _clean_row(
            raw_row,
            input_fields,
            min_content_chars,
            max_content_chars,
            min_year,
            as_of_date,
            future_tolerance_days,
        )
        row["_qc_row_index"] = index
        processed_rows.append(row)
        flag_counts.update(set(flags))
        for field in (
            "title",
            "content",
            "publish_date",
            "author",
            "source",
            "link",
            "keyword",
            "category",
            "query_group",
            "sourcefile",
            "scraped_at",
            "url_normalized",
            "article_id",
            "content_hash",
        ):
            if not (raw_row.get(field) or "").strip():
                missing_counts[field] += 1
        if parsed_date is not None:
            parsed_dates.append(parsed_date)
        title_lengths.append(row["title_length"])
        content_lengths.append(row["content_length"])
        content_word_counts.append(row["content_word_count"])
        query_counts[row.get("query_group", "") or "(kosong)"] += 1
        source_counts[row.get("source", "") or "(kosong)"] += 1

        article_id = row.get("article_id", "").strip()
        content_hash = row.get("content_hash", "").strip()
        if article_id:
            article_rows[article_id].append(row)
        if content_hash:
            content_rows[content_hash].append(row)
            if row.get("url_normalized"):
                content_urls[content_hash].add(row["url_normalized"])

    multirow_article_ids = {
        article_id: rows
        for article_id, rows in article_rows.items()
        if len(rows) > 1
    }
    multirow_content_hashes = {
        content_hash: rows
        for content_hash, rows in content_rows.items()
        if len(rows) > 1
    }
    cross_url_content_hashes = {
        content_hash: rows
        for content_hash, rows in multirow_content_hashes.items()
        if len(content_urls[content_hash]) > 1
    }
    multi_query_articles = sum(
        len({row.get("query_group", "") for row in rows}) > 1
        for rows in article_rows.values()
    )

    for rows in multirow_article_ids.values():
        for row in rows:
            row["duplicate_article_count"] = len(rows)
            row["qc_flags"] = _append_flag(row["qc_flags"], "repeated_article_id")
    for rows in multirow_content_hashes.values():
        for row in rows:
            row["duplicate_content_hash_count"] = len(rows)
            row["qc_flags"] = _append_flag(row["qc_flags"], "repeated_content_hash")
    for content_hash, rows in cross_url_content_hashes.items():
        for row in rows:
            row["content_duplicate_url_count"] = len(content_urls[content_hash])
            row["qc_flags"] = _append_flag(
                row["qc_flags"], "same_content_different_urls"
            )

    for row in processed_rows:
        row.pop("_qc_row_index", None)
        row.setdefault("duplicate_article_count", 1)
        row.setdefault("duplicate_content_hash_count", 1)
        row.setdefault("content_duplicate_url_count", 1)
    flag_counts = Counter(
        flag
        for row in processed_rows
        for flag in set(filter(None, row["qc_flags"].split(";")))
    )
    status_counts = Counter(row["qc_status"] for row in processed_rows)
    issues = [row for row in processed_rows if row["qc_status"] != "pass"]

    added_fields = (
        "title_cleaned",
        "content_cleaned",
        "publish_date_normalized",
        "title_length",
        "content_length",
        "content_word_count",
        "date_parse_ok",
        "url_valid",
        "duplicate_article_count",
        "duplicate_content_hash_count",
        "content_duplicate_url_count",
        "qc_flags",
        "qc_status",
    )
    output_fields = list(input_fields) + [
        field for field in added_fields if field not in input_fields
    ]
    issue_fields = [
        field
        for field in (
            "sourcefile",
            "query_group",
            "article_id",
            "title",
            "publish_date",
            "publish_date_normalized",
            "link",
            "content_length",
            "content_word_count",
            "qc_status",
            "qc_flags",
        )
        if field in output_fields
    ]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "newswatch_preprocessed.csv", processed_rows, output_fields)
    _write_csv(output_dir / "qc_issues.csv", issues, issue_fields)

    total = len(processed_rows)
    report = {
        "input_file": str(input_path),
        "rows_read": len(raw_rows),
        "rows_written": len(processed_rows),
        "rows_with_qc_issues": len(issues),
        "status_counts": dict(status_counts),
        "thresholds": {
            "min_content_chars": min_content_chars,
            "max_content_chars": max_content_chars,
            "min_year": min_year,
            "as_of_date": as_of_date.isoformat(),
            "future_tolerance_days": future_tolerance_days,
        },
        "missing_values": {
            field: {"count": count, "percent": _percent(count, total)}
            for field, count in sorted(missing_counts.items())
        },
        "quality_flags": {
            flag: {"count": count, "percent": _percent(count, total)}
            for flag, count in sorted(flag_counts.items())
        },
        "publication_dates": {
            "parsed_count": len(parsed_dates),
            "invalid_nonempty_count": flag_counts["invalid_publish_date"],
            "min": min(item.date() for item in parsed_dates).isoformat()
            if parsed_dates
            else None,
            "max": max(item.date() for item in parsed_dates).isoformat()
            if parsed_dates
            else None,
        },
        "length_profiles": {
            "title_characters": _length_profile(title_lengths),
            "content_characters": _length_profile(content_lengths),
            "content_words": _length_profile(content_word_counts),
            "content_below_minimum": flag_counts["content_too_short"],
            "content_above_maximum": flag_counts["content_too_long"],
        },
        "duplicates": {
            "article_id_groups_with_multiple_rows": len(multirow_article_ids),
            "rows_in_repeated_article_id_groups": sum(
                len(rows) for rows in multirow_article_ids.values()
            ),
            "articles_in_multiple_query_groups": multi_query_articles,
            "content_hash_groups_with_multiple_rows": len(multirow_content_hashes),
            "rows_in_repeated_content_hash_groups": sum(
                len(rows) for rows in multirow_content_hashes.values()
            ),
            "content_groups_with_different_urls": len(cross_url_content_hashes),
            "rows_in_different_url_content_groups": sum(
                len(rows) for rows in cross_url_content_hashes.values()
            ),
        },
        "distributions": {
            "query_group": dict(sorted(query_counts.items())),
            "top_20_sources": dict(source_counts.most_common(20)),
        },
        "row_count_matches": len(raw_rows) == len(processed_rows),
        "output_files": ["newswatch_preprocessed.csv", "qc_issues.csv"],
    }
    with (output_dir / "qc_profile_report.json").open(
        "w", encoding="utf-8"
    ) as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")

    logging.info("Membaca %s baris dari %s", len(raw_rows), input_path)
    logging.info("Menulis hasil QC ke %s", output_dir)
    return report


def _append_flag(flags, flag):
    values = [value for value in flags.split(";") if value]
    if flag not in values:
        values.append(flag)
    return ";".join(values)


def _parse_iso_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Tanggal harus berformat YYYY-MM-DD"
        ) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preprocessing dan quality control CSV Newswatch."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--min-content-chars", type=int, default=100)
    parser.add_argument("--max-content-chars", type=int, default=100000)
    parser.add_argument("--min-year", type=int, default=1900)
    parser.add_argument("--as-of-date", type=_parse_iso_date, default=date.today())
    parser.add_argument("--future-tolerance-days", type=int, default=0)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    report = run_qc(
        input_path=args.input,
        output_dir=args.output_dir,
        min_content_chars=args.min_content_chars,
        max_content_chars=args.max_content_chars,
        min_year=args.min_year,
        as_of_date=args.as_of_date,
        future_tolerance_days=args.future_tolerance_days,
    )
    print(f"Baris diproses: {report['rows_read']}")
    print(f"Status: {report['status_counts']}")
    print(f"Tanggal publikasi: {report['publication_dates']}")
    print(f"Konten < {args.min_content_chars} karakter: {report['length_profiles']['content_below_minimum']}")
    print(
        "Baris perlu ditinjau: "
        f"{report['rows_with_qc_issues']}"
    )
    print(f"Laporan: {args.output_dir / 'qc_profile_report.json'}")
    return 0 if report["row_count_matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
