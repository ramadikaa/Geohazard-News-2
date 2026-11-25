import unittest
import json

from src.gabungkan_news_watch import (
    apply_content_duplicate_labels,
    find_content_duplicate_groups,
    generate_article_id,
    generate_content_hash,
    normalize_url,
)
from src.preprocessing_qc_news_watch import _clean_row
from src.evaluate_goldset_prompts import (
    HAZARD_CLASSES,
    normalize_prediction,
    parse_json_object,
)


class ConsolidationTests(unittest.TestCase):
    def test_llm_json_parser_normalizes_all_hazard_classes(self):
        text = """```json
{"relevant": true, "classes": {"gempabumi": true}, "evidence_text": "Gempa terjadi", "confidence": 0.9}
```"""
        prediction = normalize_prediction(parse_json_object(text))
        self.assertTrue(prediction["relevant"])
        self.assertTrue(prediction["classes"]["gempabumi"])
        self.assertEqual(set(prediction["classes"]), set(HAZARD_CLASSES))
        self.assertFalse(prediction["classes"]["tsunami"])

    def test_normalize_url_removes_tracking_parameters_and_fragment(self):
        url = "HTTPS://WWW.Example.COM/news/article/?utm_source=x&b=2&a=1#section"
        self.assertEqual(
            normalize_url(url),
            "https://example.com/news/article?a=1&b=2",
        )

    def test_article_and_content_hashes_are_stable(self):
        url = "https://example.com/news/article"
        content = "<p>Gempa bumi terjadi di Jawa Barat.</p>"
        self.assertEqual(generate_article_id(url), generate_article_id(url))
        self.assertEqual(generate_content_hash(content), generate_content_hash(content))

    def test_duplicate_of_is_assigned_to_every_but_one_article(self):
        articles = [
            {
                "article_id": "b",
                "url_normalized": "https://example.com/b",
                "content_hash": "same",
                "query_group": '["gempabumi"]',
                "duplicate_of": "",
            },
            {
                "article_id": "a",
                "url_normalized": "https://example.com/a",
                "content_hash": "same",
                "query_group": '["gempabumi"]',
                "duplicate_of": "",
            },
        ]
        groups = find_content_duplicate_groups(articles)
        labeled = apply_content_duplicate_labels(articles, groups)

        self.assertEqual(labeled, 1)
        by_id = {article["article_id"]: article for article in articles}
        self.assertEqual(by_id["a"]["duplicate_of"], "")
        self.assertEqual(by_id["b"]["duplicate_of"], "a")

    def test_republika_metadata_does_not_mark_article_for_review(self):
        fields = [
            "title",
            "publish_date",
            "author",
            "content",
            "query_group",
            "sourcefile",
            "category",
            "source",
            "link",
            "url_normalized",
            "article_id",
            "content_hash",
        ]
        url = "https://news.republika.co.id/berita/tdo1wg328/gunung-semeru"
        content = "Gempa dan erupsi terjadi dengan abu vulkanik. " * 8
        raw_row = {
            "title": "Gunung Semeru Erupsi dengan Abu Vulkanik ke Arah Barat Daya",
            "publish_date": "2026-04-18 08:38:40",
            "author": "Unknown",
            "content": content,
            "query_group": "gunungapi",
            "sourcefile": "geohazard_01_gunungapi.csv",
            "category": "https:",
            "source": "republika.co.id",
            "link": url,
            "url_normalized": url,
            "article_id": generate_article_id(url),
            "content_hash": generate_content_hash(content),
        }
        cleaned, flags, _ = _clean_row(
            raw_row,
            fields,
            min_content_chars=100,
            max_content_chars=100000,
            min_year=1900,
            as_of_date=__import__("datetime").date.today(),
            future_tolerance_days=0,
        )
        self.assertNotIn("unknown_author", flags)
        self.assertNotIn("suspicious_category", flags)
        self.assertEqual(cleaned["qc_status"], "pass")


if __name__ == "__main__":
    unittest.main()
