#!/usr/bin/env python3
"""Weekly refresh: pull fresh active+planned class data from BigQuery (via Preset API),
regenerate the dashboard JSON and HTML. Commits are handled by the GitHub Action.

Tier classifications, preferred class counts, grad rate history, and NPS averages
come from snapshot CSVs in ../data/ -- these are updated manually when the CMA team
re-tiers or revises targets (which happens roughly quarterly).
"""

import csv
import json
import os
import sys
from datetime import datetime, date
from collections import defaultdict

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA = os.path.join(REPO, "data")
DOCS = os.path.join(REPO, "docs")

# ----------------- Preset API client (minimal) -----------------
PRESET_WORKSPACE = "https://8785e7f1.us2a.app.preset.io"
PRESET_API = "https://api.app.preset.io"
DATABASE_ID = 4  # Stepful BigQuery


def preset_auth():
    """Authenticate against Preset Management API and return an access token."""
    token = os.environ.get("PRESET_API_TOKEN")
    secret = os.environ.get("PRESET_API_SECRET")
    if not token or not secret:
        sys.exit("ERROR: PRESET_API_TOKEN and PRESET_API_SECRET must be set as env vars.")
    r = requests.post(f"{PRESET_API}/v1/auth/",
                      json={"name": token, "secret": secret},
                      timeout=30)
    r.raise_for_status()
    payload = r.json().get("payload", {})
    access_token = payload.get("access_token")
    if not access_token:
        sys.exit(f"ERROR: No access_token in Preset auth response: {r.text[:200]}")
    return access_token


def preset_query(access_token, sql, query_limit=5000):
    """Run a SQL query via Preset SQL Lab and return the row data."""
    r = requests.post(f"{PRESET_WORKSPACE}/api/v1/sqllab/execute/",
                      headers={"Authorization": f"Bearer {access_token}",
                               "Content-Type": "application/json",
                               "Referer": f"{PRESET_WORKSPACE}/"},
                      json={"database_id": DATABASE_ID, "sql": sql, "runAsync": False,
                            "queryLimit": query_limit},
                      timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"Preset query failed (status {r.status_code}): {r.text[:500]}")
    return r.json().get("data", [])


# ----------------- Pull fresh assignments from BigQuery -----------------
REQUESTED_NAMES_SQL = """,
  """.join([f"'{n}'" for n in [
    "Angela Axdahl","Limah Bahassoun","Melissa Bryant","Emmy Cintron",
    "Dwanda Conner","Felecia Kimble","Morgan Knox","Shenneace Lytle",
    "Carol-Ann Miller","Elizabeth Murphy","Jolene Shannon","Robyn Stanley",
    "Danielle Vado","Neenah West","Stephany Wood","Tera Clemons",
    "Courtney Tran","Judith Burnett","Delicia Cousin","Tamika Dismukes-Williams",
    "Stephanie Egleston","Kimesha Jones","Tracy Miller","Ebony Lovingood",
    "Shannon Navarrette","Nikki Pierce","Wandalize Rios","Elizabeth Taylor",
    "Tamera Thompson","Stephanie Welch","Robbin Young","Angel Cervantes",
    "Jennifer Bigler","Kerra Hines","Lybia Jewell","Mysheria Moore",
    "Mary Regis","Stephanie Robinson","Katelynn Chatman","Melanie Credit",
    "Rita Lowe","Bassim Riad","Eileen Domerchie","Elizabeth Terhune",
    "Emilie Craven","Kristina Pipes","Theresa Williams","Vermarie Penceal",
    "Vivian Akpan"]])

ACTIVE_QUERY = f"""
WITH wanted AS (
  SELECT name FROM UNNEST([{REQUESTED_NAMES_SQL}]) AS name
)
SELECT DISTINCT
  CONCAT(u.first_name, ' ', u.last_name) AS instructor,
  c.name AS cohort,
  g.name AS group,
  ct.days AS days,
  CAST(ct.lecture_start_time AS STRING) AS time,
  CAST(c.start_date AS STRING) AS start_date,
  CAST(c.end_date AS STRING) AS end_date,
  'Active' AS status,
  'Active' AS source
FROM wanted w
JOIN stepful-school.postgres_main_public.users u
  ON LOWER(CONCAT(u.first_name, ' ', u.last_name)) = LOWER(w.name)
JOIN stepful-school.postgres_main_public.instructors i ON i.user_id = u.id
JOIN stepful-school.postgres_main_public.groups g ON g.instructor_id = i.id
JOIN stepful-school.postgres_main_public.cohort_timings ct ON ct.id = g.cohort_timing_id
JOIN stepful-school.postgres_main_public.cohorts c ON c.id = ct.cohort_id
WHERE c.closed_at IS NULL
  AND c.end_date >= CURRENT_DATE()
  AND c.start_date <= CURRENT_DATE()
ORDER BY instructor, c.start_date
"""


def refresh_active_assignments(token):
    """Pull the currently-running classes from BigQuery and write to assignments.csv.

    Note: planned (future) assignments are kept from the existing CSV because they are
    maintained by the CMA team in the Certificates Cohorts Set Up sheet, not in BigQuery
    until the cohort actually launches.
    """
    rows = preset_query(token, ACTIVE_QUERY)
    print(f"Fetched {len(rows)} active assignment rows from BigQuery")

    # Read existing planned assignments
    existing_planned = []
    csv_path = os.path.join(DATA, "assignments.csv")
    if os.path.exists(csv_path):
        with open(csv_path) as f:
            for row in csv.DictReader(f):
                if row.get("source") == "Planned":
                    existing_planned.append(row)
    print(f"Kept {len(existing_planned)} planned assignments from snapshot")

    # Write merged CSV
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["instructor","cohort","group","days","time",
                                          "start_date","end_date","status","source"])
        w.writeheader()
        for r in rows:
            # Normalize time format (BigQuery returns "11:00:00" -> we want "11:00")
            t = r.get("time","")
            if t and ":" in t:
                parts = t.split(":")
                t = ":".join(parts[:2])
            w.writerow({
                "instructor": r["instructor"],
                "cohort": r["cohort"],
                "group": r.get("group","") or "",
                "days": r.get("days","") or "",
                "time": t,
                "start_date": r["start_date"],
                "end_date": r["end_date"],
                "status": "Active",
                "source": "Active",
            })
        for r in existing_planned:
            w.writerow(r)
    print(f"Wrote {csv_path}")


# ----------------- Build dashboard JSON + HTML -----------------
def rebuild_dashboard():
    """Run the existing build pipeline: CSV -> JSON -> HTML."""
    sys.path.insert(0, HERE)
    if "export_dashboard_json" in sys.modules:
        del sys.modules["export_dashboard_json"]
    if "build_dashboard_html" in sys.modules:
        del sys.modules["build_dashboard_html"]
    print("Building dashboard_data.json ...")
    import export_dashboard_json
    print("Building docs/index.html ...")
    import build_dashboard_html


# ----------------- Main -----------------
if __name__ == "__main__":
    print(f"Refresh started: {datetime.now().isoformat()}")
    try:
        token = preset_auth()
        refresh_active_assignments(token)
    except Exception as e:
        print(f"WARNING: Could not refresh from Preset ({e}). Using existing snapshot.", file=sys.stderr)
    rebuild_dashboard()
    print("Refresh complete.")
