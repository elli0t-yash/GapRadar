import unittest

from normalize_serp import normalize_results


class NormalizeSerpTests(unittest.TestCase):
    def test_normalizes_and_deduplicates_canonical_urls(self) -> None:
        payload = {
            "organic": [
                {
                    "rank": 1,
                    "title": "  Useful   result ",
                    "link": "HTTPS://WWW.Example.COM:443/path?utm_source=x#section",
                    "description": "A   useful snippet.",
                    "date": "Aug 20, 2026",
                },
                {
                    "rank": 2,
                    "title": "Duplicate",
                    "link": "https://www.example.com/path",
                    "description": "duplicate",
                },
            ]
        }

        records = normalize_results(payload, query="  test   query ", limit=10)

        self.assertEqual(
            records,
            [
                {
                    "query": "test query",
                    "title": "Useful result",
                    "url": "https://www.example.com/path",
                    "domain": "example.com",
                    "snippet": "A useful snippet.",
                    "position": 1,
                    "published_at": "2026-08-20",
                }
            ],
        )

    def test_skips_malformed_rows_and_honors_limit(self) -> None:
        payload = {
            "organic": [
                {"rank": 1, "title": "No URL"},
                {"rank": 2, "title": "Bad URL", "link": "javascript:alert(1)"},
                {"rank": "3", "title": "First", "link": "https://one.test"},
                {"rank": 4, "title": "Second", "link": "https://two.test"},
            ]
        }

        records = normalize_results(payload, query="query", limit=1)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["position"], 3)
        self.assertEqual(records[0]["published_at"], None)

    def test_empty_or_missing_organic_results_are_clean(self) -> None:
        self.assertEqual(normalize_results({}, query="query", limit=10), [])
        self.assertEqual(
            normalize_results({"organic": "bad"}, query="query", limit=10), []
        )

    def test_rejects_invalid_input_contract(self) -> None:
        with self.assertRaises(ValueError):
            normalize_results({}, query=" ", limit=10)
        with self.assertRaises(ValueError):
            normalize_results({}, query="query", limit=11)
        with self.assertRaises(ValueError):
            normalize_results({}, query="query", limit=True)


if __name__ == "__main__":
    unittest.main()
