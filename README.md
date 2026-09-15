# Scheduled Earthquake Ingestion Pipeline

A scheduled pipeline that pulls earthquake data from the USGS public feed,
stores it, and emails a digest when something significant happens.

## What it does

- Runs every 12 hours and fetches new or updated earthquakes from USGS.
- Stores every record in SQLite — no magnitude threshold on ingestion.
- Sends an email digest via Microsoft Graph whenever the run includes a
  magnitude 4.5+ earthquake.

## How it works

- **Idempotent upserts.** Each row is keyed on USGS's own event ID, so
  re-running the same window updates existing rows instead of duplicating
  them.
- **Watermarking.** The pipeline tracks the last processed timestamp and
  always fetches from there, so a missed run doesn't silently lose data.
- **Retries** on the USGS fetch with exponential backoff for transient
  failures.
- **Email delivery** goes through Microsoft Graph, authenticated as an
  Azure AD app whose Mail.Send access is scoped to a single mailbox via
  Exchange RBAC — not granted tenant-wide.

Authentication, the scoped Mail.Send authorization, and actual email
delivery have all been verified working end to end.

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in values, never commit .env
python pipeline.py          # one run
python test_idempotency.py  # idempotency check
```

## Status

Ingestion, storage, and email delivery work locally today. Scheduled cloud
deployment (Azure Container Apps Jobs) is not built yet.

## Security

Secrets live only in `.env`, which is git-ignored and never committed.
Mail-send access is deliberately scoped to one mailbox rather than granted
across the whole tenant.
