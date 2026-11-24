#!/usr/bin/env python3
"""
hard_paywall_puller.py
----------------------
Pull Amar Ujala hard-paywall funnel metrics from Pinot and write to a Google Sheet.
Idempotent per date — re-running the same date replaces that date's rows.

Usage (local or via GitHub Actions):
  python hard_paywall_puller.py                       # pulls yesterday (IST)
  python hard_paywall_puller.py 2026-11-01            # single date
  python hard_paywall_puller.py 2026-11-01 2026-11-24 # backfill range inclusive

Required env:
  PINOT_HOST          e.g. http://46.62.191.123:8099  (broker HTTP port — NOT 9000 UI port)
  PINOT_USER
  PINOT_PASSWORD
  SHEET_ID            Google Sheet ID from the URL
  GOOGLE_CREDS_JSON   Service account JSON (as a single-line string)

Optional env:
  HOME_FEED_ID        default 1838
  POSITIONS           default "5,13,21,27,28"
  URL_PREFIX          default https://www.amarujala.com/
"""

import os
import sys
import json
import time
import datetime as dt
from typing import List

import pandas as pd
import requests
from google.oauth2 import service_account
import gspread


# ---------- config ----------
PINOT_HOST        = os.environ["PINOT_HOST"].rstrip("/")
PINOT_USER        = os.environ["PINOT_USER"]
PINOT_PASSWORD    = os.environ["PINOT_PASSWORD"]
SHEET_ID          = os.environ["SHEET_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]

HOME_FEED_ID = int(os.environ.get("HOME_FEED_ID", "1838"))
POSITIONS    = [int(x) for x in os.environ.get("POSITIONS", "5,13,21,27,28").split(",")]
URL_PREFIX   = os.environ.get("URL_PREFIX", "https://www.amarujala.com/")

PINOT_QUERY_URL = f"{PINOT_HOST}/query/sql"
TIMEOUT = 180


# ---------- Pinot client ----------
def pinot_query(sql: str) -> pd.DataFrame:
    """Run a Pinot SQL query and return a DataFrame."""
    r = requests.post(
        PINOT_QUERY_URL,
        json={"sql": sql},
        auth=(PINOT_USER, PINOT_PASSWORD),
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    d = r.json()
    if d.get("exceptions"):
        raise RuntimeError(f"Pinot error: {d['exceptions']}\nSQL: {sql[:400]}")
    rt = d.get("resultTable")
    if not rt:
        return pd.DataFrame()
    cols = rt["dataSchema"]["columnNames"]
    rows = rt.get("rows", [])
    return pd.DataFrame(rows, columns=cols)


# ---------- extractors ----------
def pull_feed_metrics(date_str: str) -> pd.DataFrame:
    """Position × version × story grain. FI/FC unique users + raw events."""
    pos_list = ",".join(str(p) for p in POSITIONS)
    sql = f"""
    SELECT
      '{date_str}' AS date,
      event_hour_ist AS hour,
      feed_version_id,
      feed_position AS position,
      story_id,
      DISTINCTCOUNT(CASE WHEN event_type='FEED_IMPRESSION' THEN user_id END) AS fi_users,
      DISTINCTCOUNT(CASE WHEN event_type='FEED_CLICK'      THEN user_id END) AS fc_users,
      COUNT(CASE WHEN event_type='FEED_IMPRESSION' THEN 1 END) AS fi_events,
      COUNT(CASE WHEN event_type='FEED_CLICK'      THEN 1 END) AS fc_events
    FROM events
    WHERE event_type IN ('FEED_IMPRESSION','FEED_CLICK')
      AND feed_id = {HOME_FEED_ID}
      AND feed_position IN ({pos_list})
      AND event_date_ist = '{date_str}'
      AND screen_name = 'foryou'
      AND story_id IS NOT NULL
      AND story_id != ''
      AND story_id != 'LOYALTY'
    GROUP BY event_hour_ist, feed_version_id, feed_position, story_id
    LIMIT 100000
    """
    return pinot_query(sql)


def pull_paywall_metrics(date_str: str, story_ids: List[str]) -> pd.DataFrame:
    """Story × hour grain for PV/PC/VIL/BC/BUYS. acqSubSource substring match."""
    if not story_ids:
        return pd.DataFrame(columns=[
            "date", "hour", "story_id",
            "pv_users", "pc_users", "vil_users", "bc_users", "buys_users"
        ])
    frames = []
    for sid in story_ids:
        sql = f"""
        SELECT
          '{date_str}' AS date,
          event_hour_ist AS hour,
          '{sid}' AS story_id,
          event_type,
          DISTINCTCOUNT(user_id) AS unique_users
        FROM events
        WHERE event_date_ist = '{date_str}'
          AND event_type IN ('PAYWALL_VIEW','PAYWALL_CLICK','VIEW_ITEM_LIST','BEGIN_CHECKOUT','BUYS_PREMIUM_SUBSCRIPTION')
          AND JSON_EXTRACT_SCALAR(metadata,'$.acqSubSource','STRING','') LIKE '%{sid}%'
        GROUP BY event_hour_ist, event_type
        LIMIT 10000
        """
        df = pinot_query(sql)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=[
            "date", "hour", "story_id",
            "pv_users", "pc_users", "vil_users", "bc_users", "buys_users"
        ])
    long = pd.concat(frames, ignore_index=True)
    wide = long.pivot_table(
        index=["date", "hour", "story_id"],
        columns="event_type",
        values="unique_users",
        fill_value=0,
    ).reset_index()
    wide.columns.name = None
    rename_map = {
        "PAYWALL_VIEW": "pv_users",
        "PAYWALL_CLICK": "pc_users",
        "VIEW_ITEM_LIST": "vil_users",
        "BEGIN_CHECKOUT": "bc_users",
        "BUYS_PREMIUM_SUBSCRIPTION": "buys_users",
    }
    for src, dst in rename_map.items():
        if src in wide.columns:
            wide = wide.rename(columns={src: dst})
        else:
            wide[dst] = 0
    return wide[["date", "hour", "story_id",
                 "pv_users", "pc_users", "vil_users", "bc_users", "buys_users"]]


def pull_stories(story_ids: List[str]) -> pd.DataFrame:
    """Dimension: title_hn, author, category, url, is_premium per story_id."""
    if not story_ids:
        return pd.DataFrame(columns=[
            "story_id", "title_hn", "title_en", "author",
            "category_hn", "url", "is_premium"
        ])
    ids_sql = "','".join(story_ids)
    sql = f"""
    SELECT
      story_id,
      title_hn,
      title,
      author_firstname_hn,
      author_lastname_hn,
      category_title_hn,
      full_slug,
      is_premium
    FROM stories
    WHERE story_id IN ('{ids_sql}')
    LIMIT 10000
    """
    df = pinot_query(sql)
    if df.empty:
        return pd.DataFrame(columns=[
            "story_id", "title_hn", "title_en", "author",
            "category_hn", "url", "is_premium"
        ])
    df["author"] = (df["author_firstname_hn"].fillna("") + " " +
                    df["author_lastname_hn"].fillna("")).str.strip()
    df["url"] = URL_PREFIX + df["full_slug"].fillna("")
    out = df[["story_id", "title_hn", "title", "author",
              "category_title_hn", "url", "is_premium"]].rename(
        columns={"title": "title_en", "category_title_hn": "category_hn"}
    )
    # one row per story (first wins)
    return out.drop_duplicates(subset=["story_id"], keep="first")


# ---------- Google Sheets I/O ----------
_gc = None

def _client() -> gspread.Client:
    global _gc
    if _gc is None:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(GOOGLE_CREDS_JSON),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        _gc = gspread.authorize(creds)
    return _gc


def _open_ws(tab: str, cols: int) -> "gspread.Worksheet":
    sh = _client().open_by_key(SHEET_ID)
    try:
        return sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(tab, rows=1000, cols=cols)


def upsert_dated(tab: str, df: pd.DataFrame, cols: List[str]) -> None:
    """Replace rows for df['date'].unique() values; preserve other dates."""
    ws = _open_ws(tab, len(cols))
    existing_raw = ws.get_all_records()
    existing = pd.DataFrame(existing_raw)
    if not existing.empty and "date" in existing.columns and not df.empty:
        dates_new = set(df["date"].astype(str).unique())
        existing = existing[~existing["date"].astype(str).isin(dates_new)]
    combined = pd.concat(
        [existing[cols] if not existing.empty else pd.DataFrame(columns=cols),
         df[cols]],
        ignore_index=True,
    )
    _write(ws, combined, cols)


def upsert_dim(tab: str, df: pd.DataFrame, key: str, cols: List[str]) -> None:
    """Upsert by key column (e.g. story_id); keeps older rows intact."""
    ws = _open_ws(tab, len(cols))
    existing_raw = ws.get_all_records()
    existing = pd.DataFrame(existing_raw)
    if not existing.empty and key in existing.columns and not df.empty:
        new_keys = set(df[key].astype(str).unique())
        existing = existing[~existing[key].astype(str).isin(new_keys)]
    combined = pd.concat(
        [existing[cols] if not existing.empty else pd.DataFrame(columns=cols),
         df[cols]],
        ignore_index=True,
    )
    _write(ws, combined, cols)


def _write(ws, df: pd.DataFrame, cols: List[str]) -> None:
    ws.clear()
    data = [cols] + df.astype(str).fillna("").values.tolist() if not df.empty else [cols]
    ws.update(data, value_input_option="RAW")


# ---------- orchestration ----------
FEED_COLS = ["date", "hour", "feed_version_id", "position", "story_id",
             "fi_users", "fc_users", "fi_events", "fc_events"]
PW_COLS   = ["date", "hour", "story_id",
             "pv_users", "pc_users", "vil_users", "bc_users", "buys_users"]
DIM_COLS  = ["story_id", "title_hn", "title_en", "author",
             "category_hn", "url", "is_premium"]


def pull_one_date(date_str: str) -> None:
    print(f"\n[{date_str}] fetching feed metrics ...")
    feed_df = pull_feed_metrics(date_str)
    print(f"[{date_str}]   feed rows: {len(feed_df)}")

    if feed_df.empty:
        print(f"[{date_str}] no feed rows for this date — writing empty + skipping funnel/dim")
        upsert_dated("feed_metrics", feed_df.reindex(columns=FEED_COLS), FEED_COLS)
        return

    story_ids = sorted(feed_df["story_id"].astype(str).unique().tolist())
    print(f"[{date_str}]   unique stories: {len(story_ids)}")

    print(f"[{date_str}] fetching paywall metrics ...")
    pw_df = pull_paywall_metrics(date_str, story_ids)
    print(f"[{date_str}]   paywall rows: {len(pw_df)}")

    print(f"[{date_str}] fetching story dim ...")
    dim_df = pull_stories(story_ids)
    print(f"[{date_str}]   dim rows: {len(dim_df)}")

    print(f"[{date_str}] writing to sheet ...")
    upsert_dated("feed_metrics",    feed_df.reindex(columns=FEED_COLS), FEED_COLS)
    upsert_dated("paywall_metrics", pw_df.reindex(columns=PW_COLS),     PW_COLS)
    upsert_dim("stories_dim", dim_df, key="story_id", cols=DIM_COLS)
    print(f"[{date_str}] done.")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        ist_now = dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)
        start = end = (ist_now - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    elif len(args) == 1:
        start = end = args[0]
    elif len(args) == 2:
        start, end = args[0], args[1]
    else:
        print("usage: python hard_paywall_puller.py [date] [end_date]")
        sys.exit(1)

    d0 = dt.date.fromisoformat(start)
    d1 = dt.date.fromisoformat(end)
    if d1 < d0:
        d0, d1 = d1, d0

    print(f"range: {d0} .. {d1}  (host={PINOT_HOST}, sheet={SHEET_ID})")
    cur = d0
    while cur <= d1:
        try:
            pull_one_date(cur.strftime("%Y-%m-%d"))
        except Exception as e:
            print(f"!! {cur}: {e}")
        cur += dt.timedelta(days=1)
        time.sleep(1)  # be gentle on Pinot

    print("\nALL DONE.")


if __name__ == "__main__":
    main()
