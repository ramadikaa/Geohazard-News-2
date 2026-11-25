"""Clean, deduplicate, and profile article-level Newswatch data."""

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
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = (
    ROOT
    / "data"
    / "interim"
    / "quality_control"
    / "newswatch_preprocessed.csv"
)
DEFAULT_OUTPUT_DIR = ROOT / "data" / "interim" / "deduplication"
DEFAULT_PROCESSED_DIR = ROOT / "data" / "processed"
TOKEN_PATTERN = re.compile(r"(?u)\b\w\w+\b")
WHITESPACE_PATTERN = re.compile(r"\s+")
HTML_TAG_PATTERN = re.compile(r"<[^>]*>")
INDONESIAN_WORDS = {
    "akan", "agar", "atau", "atas", "bahwa", "bagi", "banyak", "belum",
    "dalam", "dan", "dapat", "dari", "dengan", "di", "hingga", "ini",
    "itu", "juga", "karena", "ke", "kepada", "ketika", "lebih", "masih",
    "menjadi", "menurut", "menyebut", "oleh", "pada", "saat", "sebagai",
    "sebuah", "sejumlah", "selama", "setelah", "telah", "terjadi",
    "terhadap", "tersebut", "tidak", "untuk", "yang", "kembali",
}
ENGLISH_WORDS = {
    "about", "after", "all", "also", "and", "are", "as", "at", "be",
    "been", "before", "by", "can", "for", "from", "had", "has", "have",
    "he", "her", "his", "in", "into", "is", "it", "its", "more", "not",
    "of", "on", "or", "said", "she", "that", "the", "their", "there",
    "they", "this", "to", "was", "were", "which", "will", "with",
}
TFIDF_STOPWORDS = INDONESIAN_WORDS | ENGLISH_WORDS | {
    "news", "com", "co", "id", "https", "http", "www", "kami", "anda",
    "saya", "para", "nya", "dia", "kami", "sudah", "dapat", "dilakukan",
}
ENCODING_REPLACEMENTS = {
    "\u0091": "'",
    "\u0092": "'",
    "\u0093": '"',
    "\u0094": '"',
    "\u0096": "-",
    "\u0097": "-",
    "\u00a0": " ",
    "\ufffd": " ",
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2026": "...",
    "\u200b": "",
    "\ufeff": "",
}
BOILERPLATE_LABELS = re.compile(
    r"\b(?:baca\s+juga|baca\s+selengkapnya|simak\s+selengkapnya)\s*:?",
    re.IGNORECASE,
)
TRAILING_BOILERPLATE = re.compile(
    r"(?is)(?:"
    r"\b(?:simak|baca)\s+(?:berita\s+)?selengkapnya\b.*"
    r"|\b(?:artikel|berita)\s+ini\s+telah\s+tayang\b.*"
    r"|\b(?:copyright|hak\s+cipta)\b.*"
    r"|\bsubscribe\s+to\s+continue\s+reading\b.*"
    r")$"
)
FOOTER_LINE = re.compile(
    r"^\s*(?:ADVERTISEMENT|ADVERTORIAL|IKLAN|SPONSORED\s+CONTENT|"
    r"FOLLOW\s+US\b|IKUTI\s+KAMI\b|BERLANGGANAN\s+UNTUK\s+MEMBACA\b)\b",
    re.IGNORECASE,
)
BOILERPLATE_LINE = re.compile(
    r"^\s*(?:BACA\s+JUGA\b|BACA\s+SELENGKAPNYA\b|"
    r"SIMAK\s+BERITA\s+SELENGKAPNYA\b)",
    re.IGNORECASE,
)
class _ArticleTextParser(HTMLParser):
    """Extract readable text while skipping script and style contents."""

    BLOCK_TAGS = {
        "article", "br", "div", "footer", "h1", "h2", "h3", "li", "p",
        "section", "tr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)


class _UnionFind:
    def __init__(self, values):
        self.parent = {value: value for value in values}

    def find(self, value):
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left, right):
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root

    def groups(self):
        grouped = defaultdict(list)
        for value in self.parent:
            grouped[self.find(value)].append(value)
        return list(grouped.values())


def _strip_html(value):
    parser = _ArticleTextParser()
    parser.feed(value or "")
    parser.close()
    return "".join(parser.parts)


def clean_content(value):
    """Remove markup, common scrape boilerplate, control chars, and mojibake."""
    raw = value or ""
    text = unicodedata.normalize("NFKC", html.unescape(_strip_html(raw)))
    for bad_character, replacement in ENCODING_REPLACEMENTS.items():
        text = text.replace(bad_character, replacement)
    text = "".join(
        character
        if character in "\n\t" or unicodedata.category(character) != "Cc"
        else " "
        for character in text
    )

    cleaned_lines = []
    for line in text.splitlines():
        if FOOTER_LINE.match(line):
            break
        if BOILERPLATE_LINE.match(line):
            continue
        cleaned_lines.append(line)
    text = "\n".join(cleaned_lines)
    text = TRAILING_BOILERPLATE.sub(" ", text)
    text = BOILERPLATE_LABELS.sub(" ", text)
    text = re.sub(r"(?im)\b(?:ADVERTISEMENT|ADVERTORIAL)\b", " ", text)
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def _content_fingerprint(value):
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = HTML_TAG_PATTERN.sub(" ", text)
    text = "".join(
        " " if unicodedata.category(character).startswith("P") else character
        for character in text
    )
    text = WHITESPACE_PATTERN.sub(" ", text).strip()
    return (
        hashlib.sha256(text.encode("utf-8")).hexdigest()
        if text
        else ""
    )


def detect_language(value):
    """Conservative Indonesian/English language heuristic; uncertain text is und."""
    text = (value or "").casefold()
    letters = [
        character
        for character in text
        if unicodedata.category(character).startswith("L")
    ]
    non_latin = sum(
        ord(character) > 0x024F
        and not 0x1E00 <= ord(character) <= 0x1EFF
        for character in letters
    )
    if letters and non_latin / len(letters) > 0.2:
        return "other", round(non_latin / len(letters), 3)

    tokens = TOKEN_PATTERN.findall(text[:12000])
    counts = Counter(tokens)
    id_score = sum(counts[word] for word in INDONESIAN_WORDS)
    en_score = sum(counts[word] for word in ENGLISH_WORDS)
    total = id_score + en_score
    if total < 5:
        return "und", round(total / max(len(tokens), 1), 3)
    confidence = max(id_score, en_score) / total
    if id_score > en_score and confidence >= 0.60:
        return "id", round(confidence, 3)
    if en_score > id_score and confidence >= 0.60:
        return "en", round(confidence, 3)
    return "und", round(confidence, 3)


def _parse_month(value):
    text = (value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime(
            "%Y-%m"
        )
    except ValueError:
        return ""


def _read_articles(path):
    rows = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"CSV tidak memiliki header: {path}")
        fields = [field.strip() for field in reader.fieldnames]
        required = {"article_id", "title", "content", "url_normalized"}
        missing = sorted(required - set(fields))
        if missing:
            raise ValueError(
                f"Kolom wajib tidak tersedia di {path}: {', '.join(missing)}"
            )
        for raw in reader:
            rows.append(
                {
                    field: (raw.get(original) or "")
                    for field, original in zip(fields, reader.fieldnames)
                }
            )
    ids = [row["article_id"].strip() for row in rows]
    if any(not article_id for article_id in ids):
        raise ValueError("article_id kosong ditemukan; proses dihentikan.")
    if len(ids) != len(set(ids)):
        raise ValueError(
            "article_id input tidak unik; jalankan kembali penggabungan fase 1."
        )
    return fields, rows


def _completeness_key(row):
    return (
        len(row.get("content_clean", "")),
        len(row.get("title_clean", "")),
        sum(
            bool((row.get(field) or "").strip())
            for field in ("publish_date", "source", "author", "link")
        ),
        row["article_id"],
    )


def _exact_duplicate_groups(rows):
    by_id = {row["article_id"]: row for row in rows}
    union_find = _UnionFind(by_id)
    group_fields = (
        ("exact_url", "url_normalized"),
        ("exact_content_hash", "content_hash"),
        ("exact_clean_content", "content_clean_hash"),
    )
    for reason, field in group_fields:
        grouped = defaultdict(list)
        for row in rows:
            value = (row.get(field) or "").strip()
            if value:
                grouped[value].append(row["article_id"])
        for ids in grouped.values():
            if len(ids) < 2:
                continue
            first = ids[0]
            for duplicate_id in ids[1:]:
                union_find.union(first, duplicate_id)

    duplicate_pairs = []
    representatives = set()
    for group in union_find.groups():
        representative = max(
            (by_id[article_id] for article_id in group),
            key=_completeness_key,
        )["article_id"]
        representatives.add(representative)
        for article_id in group:
            if article_id == representative:
                continue
            member = by_id[article_id]
            root = by_id[representative]
            if member["url_normalized"] == root["url_normalized"]:
                reason = "exact_url"
            elif (
                member.get("content_hash")
                and member.get("content_hash") == root.get("content_hash")
            ):
                reason = "exact_content_hash"
            elif member["content_clean_hash"] == root["content_clean_hash"]:
                reason = "exact_clean_content"
            else:
                reason = "transitive_exact_content"
            member["duplicate_of"] = representative
            member["duplicate_type"] = reason
            member["duplicate_similarity"] = "1.0"
            member["duplicate_review_status"] = "confirmed_exact"
            duplicate_pairs.append(
                _pair_record(root, member, reason, 1.0)
            )
    return representatives, duplicate_pairs


def _tokens_for_row(row, max_tokens=12000):
    title_tokens = [
        token
        for token in TOKEN_PATTERN.findall(row.get("title_clean", "").casefold())
        if token not in TFIDF_STOPWORDS
    ]
    body_tokens = [
        token
        for token in TOKEN_PATTERN.findall(row.get("content_clean", "").casefold())
        if token not in TFIDF_STOPWORDS
    ][:max_tokens]
    counts = Counter(body_tokens)
    counts.update(title_tokens * 4)
    return counts


def tfidf_candidates(rows, article_ids, threshold, max_df_ratio):
    """Generate exact sparse TF-IDF cosine candidates without all-pairs storage."""
    rows_by_id = {row["article_id"]: row for row in rows}
    documents = [rows_by_id[article_id] for article_id in article_ids]
    term_counts = [_tokens_for_row(row) for row in documents]
    document_frequency = Counter()
    for counts in term_counts:
        document_frequency.update(counts.keys())

    document_total = len(documents)
    maximum_df = max(2, int(document_total * max_df_ratio))
    inverse_document_frequency = {
        term: math.log((1 + document_total) / (1 + frequency)) + 1
        for term, frequency in document_frequency.items()
        if 2 <= frequency <= maximum_df
    }

    vectors = []
    postings = defaultdict(list)
    for index, counts in enumerate(term_counts):
        weighted = {
            term: (1 + math.log(count)) * inverse_document_frequency[term]
            for term, count in counts.items()
            if term in inverse_document_frequency
        }
        norm = math.sqrt(sum(weight * weight for weight in weighted.values()))
        if norm:
            weighted = {
                term: weight / norm for term, weight in weighted.items()
            }
        vectors.append(weighted)
        for term, weight in weighted.items():
            postings[term].append((index, weight))

    candidates = []
    for left_index, vector in enumerate(vectors):
        dot_products = defaultdict(float)
        for term, left_weight in vector.items():
            for right_index, right_weight in postings[term]:
                if right_index > left_index:
                    dot_products[right_index] += left_weight * right_weight
        for right_index, similarity in dot_products.items():
            if similarity >= threshold:
                candidates.append(
                    (
                        article_ids[left_index],
                        article_ids[right_index],
                        min(similarity, 1.0),
                    )
                )
    candidates.sort(key=lambda item: (-item[2], item[0], item[1]))
    return candidates


def _pair_record(left, right, duplicate_type, similarity):
    return {
        "article_id": right["article_id"],
        "duplicate_of": left["article_id"],
        "duplicate_type": duplicate_type,
        "similarity": round(similarity, 6),
        "review_status": (
            "pending" if duplicate_type == "near_duplicate" else "confirmed_exact"
        ),
        "title": right.get("title_clean", ""),
        "duplicate_title": left.get("title_clean", ""),
        "url_normalized": right.get("url_normalized", ""),
        "duplicate_url_normalized": left.get("url_normalized", ""),
        "query_group": right.get("query_group", ""),
        "duplicate_query_group": left.get("query_group", ""),
    }


def _read_manual_reviews(review_path):
    decisions = {}
    notes = {}
    if not review_path:
        return decisions, notes
    with Path(review_path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {
            "article_id",
            "candidate_article_id",
            "manual_review_status",
        }
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                f"Kolom review manual tidak lengkap: {', '.join(sorted(missing))}"
            )
        for row in reader:
            status = (row.get("manual_review_status") or "pending").strip().lower()
            if status not in {
                "pending",
                "confirmed",
                "confirmed_duplicate",
                "duplicate",
                "yes",
                "rejected",
                "not_duplicate",
                "false_positive",
                "no",
            }:
                raise ValueError(f"Status review manual tidak dikenal: {status}")
            pair = frozenset(
                (
                    (row.get("article_id") or "").strip(),
                    (row.get("candidate_article_id") or "").strip(),
                )
            )
            decisions[pair] = (
                "confirmed"
                if status in {"confirmed", "confirmed_duplicate", "duplicate", "yes"}
                else "rejected"
                if status in {"rejected", "not_duplicate", "false_positive", "no"}
                else "pending"
            )
            notes[pair] = (row.get("manual_review_notes") or "").strip()
    return decisions, notes


def _assign_near_duplicates(rows, representatives, candidates, review_decisions):
    by_id = {row["article_id"]: row for row in rows}
    adjacency = defaultdict(list)
    candidate_pairs = []
    for left_id, right_id, score in candidates:
        pair = frozenset((left_id, right_id))
        review_status = review_decisions.get(pair, "pending")
        pair_record = _pair_record(
            by_id[left_id], by_id[right_id], "near_duplicate", score
        )
        pair_record["review_status"] = review_status
        candidate_pairs.append(pair_record)
        if review_status == "confirmed":
            adjacency[left_id].append((score, right_id))
            adjacency[right_id].append((score, left_id))

    unassigned = set(representatives)
    ordered = sorted(
        representatives,
        key=lambda article_id: _completeness_key(by_id[article_id]),
        reverse=True,
    )
    for representative in ordered:
        if representative not in unassigned:
            continue
        unassigned.remove(representative)
        neighbors = sorted(
            adjacency[representative],
            key=lambda item: (-item[0], item[1]),
        )
        for score, duplicate_id in neighbors:
            if duplicate_id not in unassigned:
                continue
            unassigned.remove(duplicate_id)
            duplicate = by_id[duplicate_id]
            duplicate["duplicate_of"] = representative
            duplicate["duplicate_type"] = "near_duplicate"
            duplicate["duplicate_similarity"] = f"{score:.6f}"
            duplicate["duplicate_review_status"] = review_decisions.get(
                frozenset((representative, duplicate_id)), "pending"
            )
    return candidate_pairs


def _sample_candidates(candidates, by_id, sample_size, review_decisions, review_notes):
    if not candidates or sample_size <= 0:
        return []
    if len(candidates) <= sample_size:
        selected = candidates
    elif sample_size == 1:
        selected = [candidates[0]]
    else:
        selected = [
            candidates[round(index * (len(candidates) - 1) / (sample_size - 1))]
            for index in range(sample_size)
        ]
    sample = []
    for left_id, right_id, score in selected:
        left = by_id[left_id]
        right = by_id[right_id]
        pair = frozenset((left_id, right_id))
        sample.append(
            {
                "article_id": left_id,
                "candidate_article_id": right_id,
                "similarity": round(score, 6),
                "title": left.get("title_clean", ""),
                "candidate_title": right.get("title_clean", ""),
                "content_excerpt": left.get("content_clean", "")[:400],
                "candidate_content_excerpt": right.get("content_clean", "")[:400],
                "manual_review_status": review_decisions.get(pair, "pending"),
                "manual_review_notes": review_notes.get(pair, ""),
            }
        )
    return sample


def _write_csv(path, rows, fields):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_svg(path, sources, months, languages):
    sources = [
        (row["source"], row["article_count"])
        if isinstance(row, dict)
        else row
        for row in sources
    ]
    months = [
        (row["month"], row["article_count"])
        if isinstance(row, dict)
        else row
        for row in months
    ]
    languages = [
        (row["language_code"], row["article_count"])
        if isinstance(row, dict)
        else row
        for row in languages
    ]
    width, height = 1250, 780
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<style>text{font-family:Arial,sans-serif;fill:#253247}.title{font-size:25px;font-weight:bold}.heading{font-size:17px;font-weight:bold}.label{font-size:11px}.small{font-size:10px}.bar{fill:#2878b5}.bar2{fill:#48a879}.bar3{fill:#e2a23b}.axis{stroke:#b7c1cc;stroke-width:1}</style>',
        '<text x="35" y="40" class="title">Geohazard News - Ringkasan Kualitas</text>',
    ]

    source_top = sources[:12]
    parts.append('<text x="35" y="82" class="heading">12 sumber terbanyak</text>')
    max_source = max((count for _, count in source_top), default=1)
    for index, (name, count) in enumerate(source_top):
        y = 110 + index * 24
        bar_width = 330 * count / max_source
        label = html.escape(name[:30])
        parts.extend(
            [
                f'<text x="35" y="{y + 13}" class="label">{label}</text>',
                f'<rect x="245" y="{y}" width="{bar_width:.1f}" height="15" rx="2" class="bar"/>',
                f'<text x="{255 + bar_width:.1f}" y="{y + 12}" class="small">{count}</text>',
            ]
        )

    parts.append('<text x="650" y="82" class="heading">Artikel per bulan</text>')
    max_month = max((count for _, count in months), default=1)
    chart_x, chart_y, chart_h, chart_w = 660, 112, 260, 550
    parts.append(
        f'<line x1="{chart_x}" y1="{chart_y + chart_h}" x2="{chart_x + chart_w}" y2="{chart_y + chart_h}" class="axis"/>'
    )
    step = chart_w / max(len(months), 1)
    for index, (month, count) in enumerate(months):
        bar_h = chart_h * count / max_month
        x = chart_x + index * step + 1
        y = chart_y + chart_h - bar_h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(step - 2, 1):.1f}" height="{bar_h:.1f}" class="bar2"/>'
        )
        if index % 3 == 0:
            parts.append(
                f'<text x="{x:.1f}" y="{chart_y + chart_h + 18}" class="small" transform="rotate(45 {x:.1f},{chart_y + chart_h + 18})">{html.escape(month)}</text>'
            )

    parts.append('<text x="35" y="465" class="heading">Deteksi bahasa (heuristik)</text>')
    max_language = max((count for _, count in languages), default=1)
    for index, (language, count) in enumerate(languages):
        y = 495 + index * 42
        bar_width = 290 * count / max_language
        parts.extend(
            [
                f'<text x="35" y="{y + 16}" class="label">{html.escape(language)}</text>',
                f'<rect x="120" y="{y}" width="{bar_width:.1f}" height="20" rx="3" class="bar3"/>',
                f'<text x="{130 + bar_width:.1f}" y="{y + 15}" class="small">{count}</text>',
            ]
        )
    parts.append(
        '<text x="650" y="470" class="heading">Catatan</text>'
        '<text x="650" y="500" class="label">Bahasa adalah heuristik berbasis kata umum, bukan model terlatih.</text>'
        '<text x="650" y="522" class="label">Tinjau pasangan kandidat sebelum memakai label duplikat untuk analisis.</text>'
    )
    parts.append("</svg>")
    Path(path).write_text("\n".join(parts), encoding="utf-8")


def process_articles(
    input_path,
    output_dir,
    similarity_threshold=0.90,
    max_df_ratio=0.20,
    manual_sample_size=20,
    manual_review_file=None,
    processed_dir=DEFAULT_PROCESSED_DIR,
):
    if not 0 < similarity_threshold <= 1:
        raise ValueError("--similarity-threshold harus dalam rentang (0, 1].")
    if not 0 < max_df_ratio <= 1:
        raise ValueError("--max-df-ratio harus dalam rentang (0, 1].")
    if manual_sample_size < 0:
        raise ValueError("--manual-sample-size tidak boleh negatif.")

    input_fields, source_rows = _read_articles(input_path)
    rows = []
    encoding_issues = 0
    boilerplate_markers = 0
    html_rows = 0
    character_normalization_rows = 0
    source_distribution = Counter()
    monthly_distribution = Counter()
    language_distribution = Counter()

    for source_row in source_rows:
        row = dict(source_row)
        content_raw = source_row.get("content", "")
        content_clean = clean_content(content_raw)
        title_clean = clean_content(source_row.get("title", ""))
        language_code, language_confidence = detect_language(
            f"{title_clean} {content_clean[:12000]}"
        )
        row["content_raw"] = content_raw
        row["content_clean"] = content_clean
        row["content_clean_hash"] = _content_fingerprint(content_clean)
        row["title_clean"] = title_clean
        row["language_code"] = language_code
        row["language_confidence"] = language_confidence
        row["in_scope"] = str(language_code in {"id", "und"}).lower()
        row["encoding_replacement_char_count"] = content_raw.count("\ufffd")
        row["special_character_normalization_count"] = sum(
            content_raw.count(character) for character in ENCODING_REPLACEMENTS
        )
        row["content_raw_char_count"] = len(content_raw)
        row["content_clean_char_count"] = len(content_clean)
        row["html_markup_detected"] = str(
            bool(HTML_TAG_PATTERN.search(content_raw))
        ).lower()
        row["duplicate_of"] = ""
        row["duplicate_type"] = ""
        row["duplicate_similarity"] = ""
        row["duplicate_review_status"] = ""
        if row["encoding_replacement_char_count"]:
            encoding_issues += 1
        if row["special_character_normalization_count"]:
            character_normalization_rows += 1
        if HTML_TAG_PATTERN.search(content_raw):
            html_rows += 1
        if BOILERPLATE_LABELS.search(content_raw) or TRAILING_BOILERPLATE.search(
            content_raw
        ):
            boilerplate_markers += 1
        rows.append(row)
        source_distribution[row.get("source", "") or "(kosong)"] += 1
        month = _parse_month(row.get("publish_date", ""))
        if month:
            monthly_distribution[month] += 1
        language_distribution[language_code] += 1

    exact_representatives, exact_pairs = _exact_duplicate_groups(rows)
    representatives = sorted(exact_representatives)
    candidates = tfidf_candidates(
        rows,
        representatives,
        similarity_threshold,
        max_df_ratio,
    )
    review_decisions, review_notes = _read_manual_reviews(manual_review_file)
    near_pairs = _assign_near_duplicates(
        rows,
        exact_representatives,
        candidates,
        review_decisions,
    )
    duplicate_pairs = exact_pairs + near_pairs
    by_id = {row["article_id"]: row for row in rows}
    review_sample = _sample_candidates(
        candidates,
        by_id,
        manual_sample_size,
        review_decisions,
        review_notes,
    )

    input_fields = [field for field in input_fields if field != "content"]
    overlapping_output_fields = {
        "content_raw",
        "content_clean",
        "content_clean_hash",
        "title_clean",
        "language_code",
        "language_confidence",
        "in_scope",
        "encoding_replacement_char_count",
        "special_character_normalization_count",
        "content_raw_char_count",
        "content_clean_char_count",
        "html_markup_detected",
        "duplicate_of",
        "duplicate_type",
        "duplicate_similarity",
        "duplicate_review_status",
    }
    input_fields = [
        field
        for field in input_fields
        if field not in overlapping_output_fields
    ]
    output_fields = input_fields + [
        "content_raw",
        "content_clean",
        "content_clean_hash",
        "title_clean",
        "language_code",
        "language_confidence",
        "in_scope",
        "encoding_replacement_char_count",
        "special_character_normalization_count",
        "content_raw_char_count",
        "content_clean_char_count",
        "html_markup_detected",
        "duplicate_of",
        "duplicate_type",
        "duplicate_similarity",
        "duplicate_review_status",
    ]
    output_dir = Path(output_dir)
    processed_dir = Path(processed_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    indonesian_rows = [
        row for row in rows if row["language_code"] in {"id", "und"}
    ]
    prompt_rows = [
        row
        for row in indonesian_rows
        if not (row.get("duplicate_of") or "").strip()
    ]
    _write_csv(processed_dir / "articles_clean.csv", rows, output_fields)
    _write_csv(
        processed_dir / "articles_indonesia.csv",
        indonesian_rows,
        output_fields,
    )
    prompt_fields = (
        "article_id",
        "title_clean",
        "publish_date_normalized",
        "source",
        "url_normalized",
        "query_group",
        "sourcefile",
        "content_clean",
        "content_clean_hash",
        "language_code",
        "language_confidence",
    )
    _write_csv(
        processed_dir / "articles_prompt.csv",
        prompt_rows,
        prompt_fields,
    )
    pair_fields = (
        "article_id",
        "duplicate_of",
        "duplicate_type",
        "similarity",
        "review_status",
        "title",
        "duplicate_title",
        "url_normalized",
        "duplicate_url_normalized",
        "query_group",
        "duplicate_query_group",
    )
    _write_csv(
        output_dir / "duplicate_pairs.csv",
        duplicate_pairs,
        pair_fields,
    )
    review_fields = (
        "article_id",
        "candidate_article_id",
        "similarity",
        "title",
        "candidate_title",
        "content_excerpt",
        "candidate_content_excerpt",
        "manual_review_status",
        "manual_review_notes",
    )
    _write_csv(
        output_dir / "near_duplicate_manual_review.csv",
        review_sample,
        review_fields,
    )

    source_rows_out = [
        {"source": source, "article_count": count}
        for source, count in sorted(
            source_distribution.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    month_rows_out = [
        {"month": month, "article_count": monthly_distribution[month]}
        for month in sorted(monthly_distribution)
    ]
    language_rows_out = [
        {"language_code": language, "article_count": count}
        for language, count in sorted(language_distribution.items())
    ]
    _write_csv(
        output_dir / "source_distribution.csv",
        source_rows_out,
        ("source", "article_count"),
    )
    _write_csv(
        output_dir / "monthly_distribution.csv",
        month_rows_out,
        ("month", "article_count"),
    )
    _write_csv(
        output_dir / "language_distribution.csv",
        language_rows_out,
        ("language_code", "article_count"),
    )

    articles_with_duplicate_link = sum(bool(row["duplicate_of"]) for row in rows)
    review_counts = Counter(
        review_decisions.get(frozenset((left, right)), "pending")
        for left, right, _ in candidates
    )
    if not candidates:
        manual_review_state = "not_required"
    elif review_counts["pending"]:
        manual_review_state = "pending"
    else:
        manual_review_state = "completed"
    url_counts = Counter(
        row.get("url_normalized", "")
        for row in rows
        if row.get("url_normalized")
    )
    metrics = {
        "input_articles": len(rows),
        "articles_with_duplicate_of": articles_with_duplicate_link,
        "exact_duplicate_relations": len(exact_pairs),
        "unique_normalized_urls": len(url_counts),
        "exact_url_duplicate_groups": sum(
            count > 1 for count in url_counts.values()
        ),
        "near_duplicate_candidate_pairs": len(candidates),
        "near_duplicate_relations_assigned": sum(
            row["duplicate_type"] == "near_duplicate" for row in rows
        ),
        "near_duplicate_reviews": dict(review_counts),
        "similarity_threshold": similarity_threshold,
        "language_heuristic": "common Indonesian/English word counts; low-confidence text marked und",
        "language_id": language_distribution["id"],
        "language_en": language_distribution["en"],
        "language_other": language_distribution["other"],
        "language_und": language_distribution["und"],
        "articles_in_indonesia_output": len(indonesian_rows),
        "articles_in_prompt_output": len(prompt_rows),
        "duplicate_exact_articles_excluded_from_prompt": len(indonesian_rows) - len(prompt_rows),
        "processed_output_dir": str(processed_dir),
        "interim_output_dir": str(output_dir),
        "excluded_as_non_indonesian": len(rows) - len(indonesian_rows),
        "encoding_replacement_rows": encoding_issues,
        "special_character_normalization_rows": character_normalization_rows,
        "rows_with_html_markup": html_rows,
        "rows_with_boilerplate_markers": boilerplate_markers,
    }
    metric_rows = [
        {"metric": name, "value": value, "notes": ""}
        for name, value in metrics.items()
    ]
    _write_csv(
        output_dir / "quality_metrics.csv",
        metric_rows,
        ("metric", "value", "notes"),
    )

    _write_svg(
        output_dir / "quality_summary.svg",
        source_rows_out,
        month_rows_out,
        language_rows_out,
    )
    report = {
        "input_file": str(input_path),
        "rows_read": len(source_rows),
        "rows_written": len(rows),
        "text_cleaning": {
            "preserved_raw_content": True,
            "content_clean_field": "content_clean",
            "html_rows_detected": html_rows,
            "rows_with_boilerplate_markers": boilerplate_markers,
            "rows_with_unicode_replacement_char": encoding_issues,
            "rows_with_special_characters_normalized": character_normalization_rows,
            "replacement_character_policy": "U+FFFD replaced with a space in content_clean; content_raw is preserved",
        },
        "deduplication": {
            "exact_url_already_aggregated_in_phase_1": True,
            "exact_duplicate_relations": len(exact_pairs),
            "near_duplicate_method": "sparse TF-IDF cosine similarity (unigrams; title weighted 4x)",
            "similarity_threshold": similarity_threshold,
            "max_document_frequency_ratio": max_df_ratio,
            "near_duplicate_candidate_pairs": len(candidates),
            "near_duplicate_relations_assigned": metrics[
                "near_duplicate_relations_assigned"
            ],
            "articles_removed": 0,
            "duplicate_link_field": "duplicate_of",
            "manual_review_sample_size": len(review_sample),
            "manual_review_status": manual_review_state,
            "manual_review_counts": dict(review_counts),
            "manual_review_file": str(manual_review_file)
            if manual_review_file
            else None,
        },
        "language": {
            "method": metrics["language_heuristic"],
            "distribution": dict(sorted(language_distribution.items())),
            "indonesia_output_rule": "language_code is id or und; en/other are excluded from articles_indonesia.csv and retained in articles_clean.csv",
        },
        "distribution": {
            "source": dict(source_distribution.most_common()),
            "month": dict(sorted(monthly_distribution.items())),
        },
        "metrics": metrics,
        "output_files": {
            "processed": [
                "articles_clean.csv",
                "articles_indonesia.csv",
                "articles_prompt.csv",
            ],
            "interim": [
                "duplicate_pairs.csv",
                "near_duplicate_manual_review.csv",
                "quality_metrics.csv",
                "source_distribution.csv",
                "monthly_distribution.csv",
                "language_distribution.csv",
                "quality_summary.svg",
                "quality_report.json",
            ],
        },
    }
    with (output_dir / "quality_report.json").open(
        "w", encoding="utf-8"
    ) as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    logging.info("Memproses %s artikel dari %s", len(rows), input_path)
    logging.info("Menulis hasil ke %s", output_dir)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Pembersihan teks, deduplikasi, deteksi bahasa, dan profiling artikel."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--processed-dir", type=Path, default=DEFAULT_PROCESSED_DIR
    )
    parser.add_argument("--similarity-threshold", type=float, default=0.90)
    parser.add_argument("--max-df-ratio", type=float, default=0.20)
    parser.add_argument("--manual-sample-size", type=int, default=20)
    parser.add_argument(
        "--manual-review-file",
        type=Path,
        help="Review CSV sebelumnya dengan status confirmed/rejected/pending.",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )

    report = process_articles(
        args.input,
        args.output_dir,
        args.similarity_threshold,
        args.max_df_ratio,
        args.manual_sample_size,
        args.manual_review_file,
        args.processed_dir,
    )
    metrics = report["metrics"]
    print(f"Artikel diproses: {metrics['input_articles']}")
    print(f"Relasi duplikat exact: {metrics['exact_duplicate_relations']}")
    print(
        "Kandidat near-duplicate: "
        f"{metrics['near_duplicate_candidate_pairs']} "
        f"(ambang cosine >= {args.similarity_threshold:.2f})"
    )
    print(f"Artikel Indonesia/uncertain: {metrics['articles_in_indonesia_output']}")
    print(f"Baris review manual: {report['deduplication']['manual_review_sample_size']}")
    print(f"Laporan: {args.output_dir / 'quality_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
