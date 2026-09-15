"""Scheduled ingestion pipeline for USGS earthquakes.

Fetch -> upsert -> log -> digest -> send.

Sending requires GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET,
GRAPH_SENDER_MAILBOX, and GRAPH_DIGEST_RECIPIENT to be set. If any are
missing, the digest is still built and logged, just not sent.
"""
import html
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote as urlquote

import requests
from tenacity import retry, stop_after_attempt, wait_random_exponential

USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
NOTABLE_MAGNITUDE = 4.5
BACKFILL_HOURS = 24

DB_PATH = os.environ.get("DB_PATH") or str(Path(__file__).parent / "pipeline.db")

GRAPH_TENANT_ID = os.environ.get("GRAPH_TENANT_ID")
GRAPH_CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID")
GRAPH_CLIENT_SECRET = os.environ.get("GRAPH_CLIENT_SECRET")
GRAPH_SENDER_MAILBOX = os.environ.get("GRAPH_SENDER_MAILBOX")
GRAPH_DIGEST_RECIPIENT = os.environ.get("GRAPH_DIGEST_RECIPIENT")

SCHEMA = """
CREATE TABLE IF NOT EXISTS earthquakes (
    id        TEXT PRIMARY KEY,
    time      INTEGER NOT NULL,
    updated   INTEGER NOT NULL,
    magnitude REAL,
    place     TEXT,
    longitude REAL,
    latitude  REAL,
    depth     REAL
);

CREATE TABLE IF NOT EXISTS pipeline_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_db(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def get_watermark(conn):
    row = conn.execute(
        "SELECT value FROM pipeline_state WHERE key = 'watermark_updated_ms'"
    ).fetchone()
    if row:
        return int(row[0])
    # First run, no prior watermark: backfill the last 24h.
    cutoff = datetime.now(timezone.utc) - timedelta(hours=BACKFILL_HOURS)
    return int(cutoff.timestamp() * 1000)


def set_watermark(conn, watermark_ms):
    conn.execute(
        "INSERT INTO pipeline_state (key, value) VALUES ('watermark_updated_ms', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(watermark_ms),),
    )


@retry(stop=stop_after_attempt(3), wait=wait_random_exponential(multiplier=1, max=20))
def fetch_events(updatedafter_iso):
    resp = requests.get(
        USGS_URL,
        params={"format": "geojson", "updatedafter": updatedafter_iso},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["features"]


def upsert_events(conn, features):
    """Insert or update each feature by its natural (source-supplied) id.
    Returns (inserted_count, updated_count).
    """
    ids = [f["id"] for f in features]
    existing = set()
    if ids:
        placeholders = ",".join("?" for _ in ids)
        existing = {
            row[0]
            for row in conn.execute(
                f"SELECT id FROM earthquakes WHERE id IN ({placeholders})", ids
            )
        }

    inserted = updated = 0
    for f in features:
        props = f["properties"]
        lon, lat, depth = f["geometry"]["coordinates"]
        conn.execute(
            """
            INSERT INTO earthquakes (id, time, updated, magnitude, place, longitude, latitude, depth)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                time = excluded.time,
                updated = excluded.updated,
                magnitude = excluded.magnitude,
                place = excluded.place,
                longitude = excluded.longitude,
                latitude = excluded.latitude,
                depth = excluded.depth
            """,
            (f["id"], props["time"], props["updated"], props["mag"], props["place"], lon, lat, depth),
        )
        if f["id"] in existing:
            updated += 1
        else:
            inserted += 1

    return inserted, updated


def build_digest(features):
    """Notable-records HTML digest, or None if nothing meets the threshold.
    The USGS event page link is derived from `id` -- it isn't a stored field.
    """
    notable = [f for f in features if (f["properties"].get("mag") or 0) >= NOTABLE_MAGNITUDE]
    if not notable:
        return None
    rows = "\n".join(
        "<tr><td>{place}</td><td>{mag}</td><td>{time}</td>"
        "<td><a href='https://earthquake.usgs.gov/earthquakes/eventpage/{id}'>details</a></td></tr>".format(
            place=html.escape(f["properties"]["place"]),
            mag=f["properties"]["mag"],
            time=datetime.fromtimestamp(f["properties"]["time"] / 1000, tz=timezone.utc).isoformat(),
            id=urlquote(f["id"], safe=""),
        )
        for f in notable
    )
    return (
        f"<h2>{len(notable)} notable earthquake(s) (M{NOTABLE_MAGNITUDE}+)</h2>"
        f"<table border='1'><tr><th>Place</th><th>Mag</th><th>Time (UTC)</th><th>Link</th></tr>{rows}</table>"
    )


def get_graph_token():
    resp = requests.post(
        f"https://login.microsoftonline.com/{GRAPH_TENANT_ID}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": GRAPH_CLIENT_ID,
            "client_secret": GRAPH_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def send_digest_email(html):
    if not all([GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_SENDER_MAILBOX, GRAPH_DIGEST_RECIPIENT]):
        print(json.dumps({"event": "digest_not_sent", "reason": "graph_not_configured"}))
        return

    token = get_graph_token()
    message = {
        "message": {
            "subject": f"Earthquake digest: notable events (M{NOTABLE_MAGNITUDE}+)",
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": GRAPH_DIGEST_RECIPIENT}}],
        },
        "saveToSentItems": "false",
    }
    resp = requests.post(
        f"https://graph.microsoft.com/v1.0/users/{GRAPH_SENDER_MAILBOX}/sendMail",
        headers={"Authorization": f"Bearer {token}"},
        json=message,
        timeout=30,
    )
    resp.raise_for_status()
    print(json.dumps({"event": "digest_sent", "recipient": GRAPH_DIGEST_RECIPIENT}))


def run():
    start = datetime.now(timezone.utc)
    conn = get_db()
    try:
        watermark_ms = get_watermark(conn)
        watermark_iso = datetime.fromtimestamp(watermark_ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S"
        )

        features = fetch_events(watermark_iso)
        inserted, updated = upsert_events(conn, features)
        set_watermark(conn, int(start.timestamp() * 1000))
        conn.commit()

        digest_html = build_digest(features)
        if digest_html:
            send_digest_email(digest_html)

        print(json.dumps({
            "run_started": start.isoformat(),
            "watermark_used": watermark_iso,
            "fetched": len(features),
            "inserted": inserted,
            "updated": updated,
            "skipped": len(features) - inserted - updated,
            "notable": sum(1 for f in features if (f["properties"].get("mag") or 0) >= NOTABLE_MAGNITUDE),
            "duration_s": (datetime.now(timezone.utc) - start).total_seconds(),
        }))
    except Exception as exc:
        print(json.dumps({"run_started": start.isoformat(), "error": str(exc)}))
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    run()
