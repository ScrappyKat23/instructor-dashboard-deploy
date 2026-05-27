#!/usr/bin/env python3
"""Weekly refresh: pull fresh active+planned class data from BigQuery (via Preset API),
regenerate the dashboard JSON and HTML. Commits are handled by the GitHub Action.

Tier classifications, preferred class counts, grad rate history, and NPS averages
come from snapshot CSVs in ../data/ -- these are updated manually when the CMA team
re-tiers or revises targets (which happens roughly quarterly).

Capacity logic:
  - Preferred cap is MAX 6 per instructor (hard ceiling regardless of source data).
  - Over-allocation is measured via mid-month sampling (the 15th of each month)
    to avoid flagging transient overlap spikes during cohort transitions.
    A 1-week blip where a cohort ends the same week a new one starts is normal
    and should not count as sustained over-capacity.
"""

import csv
import json
import os
import sys
from datetime import datetime, date, timedelta
from collections import defaultdict

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DATA = os.path.join(REPO, "data")
DOCS = os.path.join(REPO, "docs")

MAX_PREFERRED = 6  # Hard cap: no instructor should show preferred > 6

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


def mid_month_load(assignments, year, month):
    """Count active classes for each instructor on the 15th of the given month.

    Using the 15th as a stable mid-month sample point avoids counting transient
    overlap spikes that happen when one cohort ends and another starts in the same week.
    A sustained over-allocation shows up on the 15th; a 1-week transition spike does not.
    """
    sample_date = date(year, month, 15)
    counts = defaultdict(int)
    for row in assignments:
        try:
            start = date.fromisoformat(row["start_date"])
            end = date.fromisoformat(row["end_date"])
        except (ValueError, KeyError):
            continue
        if start <= sample_date <= end:
            counts[row["instructor"]] += 1
    return counts


def compute_capacity_flags(assignments, preferred_map):
    """Return per-instructor capacity status using mid-month sampling.

    Status values:
      'over'     -- sustained over preferred on any mid-month sample in the next 6 months
      'at'       -- at preferred on at least one mid-month sample, never over
      'under'    -- consistently under preferred
      'ok'       -- no assignments in window
    """
    today = date.today()
    # Sample the 15th of the current month + next 5 months
    sample_months = []
    for delta in range(6):
        y = today.year + (today.month - 1 + delta) // 12
        m = (today.month - 1 + delta) % 12 + 1
        sample_months.append((y, m))

    status = {}
    for instructor, preferred in preferred_map.items():
        cap = min(preferred, MAX_PREFERRED)  # enforce hard cap
        over_count = 0
        at_count = 0
        for y, m in sample_months:
            load = mid_month_load(assignments, y, m).get(instructor, 0)
            if load > cap:
                over_count += 1
            elif load == cap:
                at_count += 1
        if over_count > 0:
            status[instructor] = "over"
        elif at_count > 0:
            status[instructor] = "at"
        else:
            # Check if they have any load at all
            any_load = any(
                mid_month_load(assignments, y, m).get(instructor, 0) > 0
                for y, m in sample_months
            )
            status[instructor] = "under" if any_load else "ok"
    return status


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
    return csv_path


def load_preferred_map():
    """Load instructor preferred class counts from data/instructors.csv.
    Caps all values at MAX_PREFERRED (6).
    """
    preferred_map = {}
    instructors_csv = os.path.join(DATA, "instructors.csv")
    if not os.path.exists(instructors_csv):
        print(f"WARNING: {instructors_csv} not found, skipping capacity flags.")
        return preferred_map
    with open(instructors_csv) as f:
        for row in csv.DictReader(f):
            name = row.get("instructor","").strip()
            try:
                preferred = min(int(row.get("preferred_classes", 0)), MAX_PREFERRED)
            except ValueError:
                preferred = 0
            if name:
                preferred_map[name] = preferred
    return preferred_map


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
    print(f"Capacity logic: mid-month (15th) sampling, preferred cap = {MAX_PREFERRED}")
    try:
        token = preset_auth()
        csv_path = refresh_active_assignments(token)

        # Compute and log capacity flags for visibility in Actions logs
        assignments = []
        with open(csv_path) as f:
            assignments = list(csv.DictReader(f))
        preferred_map = load_preferred_map()
        if preferred_map:
            flags = compute_capacity_flags(assignments, preferred_map)
            over = [i for i, s in flags.items() if s == "over"]
            at   = [i for i, s in flags.items() if s == "at"]
            print(f"Capacity check: {len(over)} over, {len(at)} at capacity")
            if over:
                print(f"  Over capacity (sustained): {', '.join(sorted(over))}")
    except Exception as e:
        print(f"WARNING: Could not refresh from Preset ({e}). Using existing snapshot.",
              file=sys.stderr)
    rebuild_dashboard()
    print("Refresh complete.")#!/usr/bin/env python3
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
