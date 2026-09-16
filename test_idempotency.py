"""Checks required by the project brief:
1. Upserting the same fetched batch repeatedly must not change row counts.
2. Malicious/malformed HTML in USGS-sourced fields must not alter the
   generated digest markup (output-encoding check).

Run directly: `python test_idempotency.py`
"""
import json
import sqlite3
from pathlib import Path

from pipeline import SCHEMA, build_digest, upsert_events

FIXTURE = Path(__file__).parent / "fixtures" / "sample_response.json"


def test_repeated_upsert_is_idempotent():
    features = json.loads(FIXTURE.read_text())["features"]
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)

    row_counts = []
    for _ in range(5):
        upsert_events(conn, features)
        conn.commit()
        row_counts.append(conn.execute("SELECT COUNT(*) FROM earthquakes").fetchone()[0])

    assert row_counts == [len(features)] * 5, f"row count changed across reruns: {row_counts}"
    print(f"OK: {len(features)} rows, stable across 5 reruns: {row_counts}")


def test_digest_escapes_malicious_usgs_fields():
    malicious_feature = {
        "id": "us\"'><script>alert(1)</script>",
        "properties": {
            "mag": 5.5,
            "place": "<script>alert('xss')</script> 10km SW of Nowhere",
            "time": 1700000000000,
            "updated": 1700000100000,
        },
        "geometry": {"type": "Point", "coordinates": [-122.4, 37.7, 10.0]},
    }

    html_out = build_digest([malicious_feature], inserted=1, updated=0)

    assert "Run summary: 1 fetched, 1 inserted, 0 updated, 0 skipped." in html_out
    assert "<script>" not in html_out, "raw <script> tag leaked into digest markup"
    assert "onerror=" not in html_out
    assert "&lt;script&gt;" in html_out, "place field was not HTML-escaped"
    # The malicious id must not be able to close the href attribute early.
    assert "'><script>" not in html_out, "id field broke out of the href attribute"
    print("OK: malicious USGS place/id values are safely encoded, not executable markup")


if __name__ == "__main__":
    test_repeated_upsert_is_idempotent()
    test_digest_escapes_malicious_usgs_fields()
