"""Idempotency check required by the project brief: upserting the same
fetched batch repeatedly must not change row counts.

Run directly: `python test_idempotency.py`
"""
import json
import sqlite3
from pathlib import Path

from pipeline import SCHEMA, upsert_events

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


if __name__ == "__main__":
    test_repeated_upsert_is_idempotent()
