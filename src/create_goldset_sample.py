"""Create a deterministic, stratified 200-article gold-set sample."""

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "processed" / "articles_prompt.csv"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "interim" / "goldset"

HAZARD_GROUPS = (
    "gunungapi",
    "gempabumi",
    "tsunami",
    "tanah_longsor",
    "likuefaksi",
    "abrasi_pantai",
    "penurunan_tanah",
    "semburan_gas_lumpur",
)


def parse_query_groups(value):
    if not value:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass

    if "," in value:
        return [item.strip() for item in value.split(",") if item.strip()]
    return [value.strip()] if value.strip() else []


def read_articles(input_path):
    with Path(input_path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"CSV tidak memiliki header: {input_path}")
        required = {"article_id", "title_clean", "content_clean", "query_group"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(
                f"Kolom wajib tidak tersedia di {input_path}: {', '.join(sorted(missing))}"
            )
        return reader.fieldnames, list(reader)


def deterministic_score(row, seed):
    payload = f"{seed}|{row['article_id']}|{row.get('url_normalized', '')}"
    return int(hashlib.sha256(payload.encode("utf-8")).hexdigest(), 16)


def sample_articles(rows, sample_size, seed):
    target_per_group = sample_size // len(HAZARD_GROUPS)
    if target_per_group * len(HAZARD_GROUPS) != sample_size:
        raise ValueError("sample-size harus habis dibagi jumlah kelompok bencana.")

    by_id = {}
    for row in rows:
        article_id = (row.get("article_id") or "").strip()
        if article_id:
            by_id[article_id] = row

    candidates = list(by_id.values())
    selected_ids = set()
    allocations = {}
    source_counts = Counter()

    for hazard_group in HAZARD_GROUPS:
        exact_candidates = [
            row
            for row in candidates
            if article_id_is_available(row, selected_ids)
            and parse_query_groups(row.get("query_group", "")) == [hazard_group]
        ]
        mixed_candidates = [
            row
            for row in candidates
            if article_id_is_available(row, selected_ids)
            and hazard_group in parse_query_groups(row.get("query_group", ""))
        ]

        pool = exact_candidates + [
            row
            for row in mixed_candidates
            if row not in exact_candidates
        ]
        pool.sort(
            key=lambda row: (
                source_counts[row.get("source", "") or "(kosong)"],
                -deterministic_score(row, seed),
                row["article_id"],
            )
        )

        picked = pool[:target_per_group]
        allocations[hazard_group] = len(picked)
        for row in picked:
            article_id = row["article_id"]
            selected_ids.add(article_id)
            source_counts[row.get("source", "") or "(kosong)"] += 1

    if len(selected_ids) < sample_size:
        for row in sorted(
            candidates,
            key=lambda item: deterministic_score(item, seed),
        ):
            if row["article_id"] in selected_ids:
                continue
            selected_ids.add(row["article_id"])
            if len(selected_ids) == sample_size:
                break

    return [
        by_id[article_id]
        for article_id in sorted(
            selected_ids,
            key=lambda item: deterministic_score(by_id[item], seed),
        )
    ], allocations


def article_id_is_available(row, selected_ids):
    return row["article_id"] not in selected_ids


def write_csv(path, fieldnames, rows):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def make_template_row(row):
    template = {
        field: row.get(field, "")
        for field in (
            "article_id",
            "title_clean",
            "publish_date_normalized",
            "source",
            "url_normalized",
            "query_group",
            "sourcefile",
            "content_clean",
        )
    }
    template.update(
        {
            "relevant": "",
            "gunungapi": "",
            "gempabumi": "",
            "tsunami": "",
            "tanah_longsor": "",
            "likuefaksi": "",
            "abrasi_pantai": "",
            "penurunan_tanah": "",
            "semburan_gas_lumpur": "",
            "evidence_text": "",
            "event_report_count": "",
            "goldset_uncertainty": "",
            "annotation_notes": "",
            "annotator": "",
        }
    )
    return template


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Ambil sampel deterministik untuk gold set 200 artikel."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--sample-size", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20261123)
    args = parser.parse_args(argv)

    if args.sample_size < 1:
        raise ValueError("--sample-size harus lebih dari 0")

    _, rows = read_articles(args.input)
    sampled, allocations = sample_articles(rows, args.sample_size, args.seed)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    template_fields = (
        "article_id",
        "title_clean",
        "publish_date_normalized",
        "source",
        "url_normalized",
        "query_group",
        "sourcefile",
        "content_clean",
        "relevant",
        "gunungapi",
        "gempabumi",
        "tsunami",
        "tanah_longsor",
        "likuefaksi",
        "abrasi_pantai",
        "penurunan_tanah",
        "semburan_gas_lumpur",
        "evidence_text",
        "event_report_count",
        "goldset_uncertainty",
        "annotation_notes",
        "annotator",
    )
    template_rows = [make_template_row(row) for row in sampled]
    write_csv(
        output_dir / "goldset_template.csv",
        template_fields,
        template_rows,
    )
    write_csv(
        output_dir / "goldset_source_sample.csv",
        tuple(rows[0].keys()),
        sampled,
    )

    source_counts = Counter(
        row.get("source", "") or "(kosong)" for row in sampled
    )
    report = {
        "input_file": str(args.input),
        "sample_size": len(sampled),
        "seed": args.seed,
        "target_per_hazard_group": args.sample_size // len(HAZARD_GROUPS),
        "actual_hazard_group_allocations": allocations,
        "source_distribution": dict(sorted(source_counts.items())),
        "output_files": [
            "goldset_template.csv",
            "goldset_source_sample.csv",
        ],
    }
    with (output_dir / "goldset_sampling_report.json").open(
        "w", encoding="utf-8"
    ) as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")

    print(f"Artikel sampel: {len(sampled)}")
    print(f"Output: {output_dir / 'goldset_template.csv'}")
    print(f"Laporan: {output_dir / 'goldset_sampling_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
