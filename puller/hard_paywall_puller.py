#!/usr/bin/env python3
"""
hard_paywall_puller.py  (v2)
----------------------------
v2 changes vs v1:
  - Paywall metrics now ONE query per day (splitPart on acqSubSource)
    instead of N queries per story. ~20-50x faster.
  - Accumulates all dates in memory, writes each tab ONCE at the end.
  - Forced unbuffered stdout so Actions logs stream live.

Usage:
  python -u hard_paywall_puller.py                       # pulls yesterday (IST)
  python -u hard_paywall_puller.py 2026-11-01            # single date
  python -u hard_paywall_puller.py 2026-11-01 2026-11-23 # backfill range inclusive
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


# Force unbuffered output (safety net in addition to python -u)
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)


# ---------- config ----------
PINOT_HOST        = os.environ["PINOT_HOST"].rstrip("/")
PINOT_USER        = os.environ["PINOT_USER"]
PINOT_PASSWORD    = os.environ["PINOT_PASSWORD"]
SHEET_ID          = os.environ["SHEET_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]

HOME_FEED_ID = int(os.environ.get("HOME_FEED_ID", "1838"))
POSITIONS    = [int(x) for x in os.environ.get("POSITIONS", "5,13,21,27,28").split(",")]
URL_PREFIX   = os.environ.get("URL_PREFIX", "https://www.amarujala.com/")

PINOT_QUERY_URL = f"{PINOT_HOST}/sql"   # controller endpoint (port 9000 — publicly reachable)
TIMEOUT = 180


def log(msg: str):
    print(f"[{dt.datetime.utcnow().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- Pinot ----------
def pinot_query(sql: str) -> pd.DataFrame:
    t0 = time.time()
    # NOTE: no useMultistageEngine — MSE's Calcite parser treats "date" and "hour"
    # as reserved words and rejects `... AS date`. Our queries are simple aggregates
    # so single-stage engine (default) works and is faster.
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
    took = time.time() - t0
    if not rt:
        log(f"  query returned no resultTable ({took:.1f}s)")
        return pd.DataFrame()
    cols = rt["dataSchema"]["columnNames"]
    rows = rt.get("rows", [])
    log(f"  query ok: {len(rows)} rows in {took:.1f}s")
    return pd.DataFrame(rows, columns=cols)


# ---------- extractors ----------
def pull_feed_metrics(date_str: str) -> pd.DataFrame:
    # NOTE: do NOT alias to `date`, `hour`, or `position` in SQL — all three
    # are Pinot/Calcite reserved words. Add/rename in pandas after the query.
    pos_list = ",".join(str(p) for p in POSITIONS)
    sql = f"""
    SELECT
      event_hour_ist,
      feed_version_id,
      feed_position,
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
      AND LOWER(platform) IN ('android','ios')
      AND story_id IS NOT NULL
      AND story_id != ''
      AND story_id != 'LOYALTY'
    GROUP BY event_hour_ist, feed_version_id, feed_position, story_id
    LIMIT 100000
    """
    df = pinot_query(sql)
    if df.empty:
        return df
    df["date"] = date_str
    df = df.rename(columns={"event_hour_ist": "hour", "feed_position": "position"})
    return df


def pull_paywall_metrics_day(date_str: str, keep_story_ids: set) -> pd.DataFrame:
    """
    ONE query per day. Resolves story_id for each event:
      - PAYWALL_VIEW / PAYWALL_CLICK: events.story_id flat column (populated)
      - VIEW_ITEM_LIST / BEGIN_CHECKOUT / BUYS: story_id is NULL on these events,
        so we extract the 24-char ObjectId from metadata.acqSubSource via regex.
    Pre-filtered to premium paywall events only via acqSource='pay wall | premium'
    (excludes e-paper paywall).
    """
    sql = f"""
    SELECT
      event_hour_ist,
      CASE
        WHEN story_id IS NOT NULL AND story_id != '' AND story_id != 'null'
          THEN story_id
        ELSE REGEXP_EXTRACT(JSON_EXTRACT_SCALAR(metadata, '$.acqSubSource', 'STRING', ''), '[a-f0-9]{{24}}')
      END AS resolved_story_id,
      event_type,
      DISTINCTCOUNT(user_id) AS unique_users
    FROM events
    WHERE event_date_ist = '{date_str}'
      AND LOWER(platform) IN ('android','ios')
      AND JSON_EXTRACT_SCALAR(metadata, '$.acqSource', 'STRING', '') = 'pay wall | premium'
      AND (
        (event_type IN ('PAYWALL_VIEW','PAYWALL_CLICK')
          AND screen_name = 'articleDetailScreenview')
        OR
        (event_type IN ('VIEW_ITEM_LIST','BEGIN_CHECKOUT','BUYS_PREMIUM_SUBSCRIPTION')
          AND screen_name = 'subscriptionScreen')
      )
    GROUP BY event_hour_ist,
      CASE
        WHEN story_id IS NOT NULL AND story_id != '' AND story_id != 'null'
          THEN story_id
        ELSE REGEXP_EXTRACT(JSON_EXTRACT_SCALAR(metadata, '$.acqSubSource', 'STRING', ''), '[a-f0-9]{{24}}')
      END,
      event_type
    LIMIT 200000
    """
    df = pinot_query(sql)
    if df.empty:
        return pd.DataFrame(columns=["date","hour","story_id","pv_users","pc_users","vil_users","bc_users","buys_users"])

    # add date column + rename to canonical names
    df["date"] = date_str
    df = df.rename(columns={"event_hour_ist": "hour", "resolved_story_id": "story_id"})
    # drop rows where neither flat story_id nor regex extraction found a story
    df = df[df["story_id"].astype(str).str.strip() != ""]
    df = df[df["story_id"].astype(str).str.strip() != "null"]
    if keep_story_ids:
        df = df[df["story_id"].astype(str).isin(keep_story_ids)]
    if df.empty:
        return pd.DataFrame(columns=["date","hour","story_id","pv_users","pc_users","vil_users","bc_users","buys_users"])

    wide = df.pivot_table(
        index=["date","hour","story_id"],
        columns="event_type",
        values="unique_users",
        fill_value=0,
    ).reset_index()
    wide.columns.name = None
    rename_map = {
        "PAYWALL_VIEW":"pv_users",
        "PAYWALL_CLICK":"pc_users",
        "VIEW_ITEM_LIST":"vil_users",
        "BEGIN_CHECKOUT":"bc_users",
        "BUYS_PREMIUM_SUBSCRIPTION":"buys_users",
    }
    for src, dst in rename_map.items():
        if src in wide.columns: wide = wide.rename(columns={src:dst})
        else: wide[dst] = 0
    return wide[["date","hour","story_id","pv_users","pc_users","vil_users","bc_users","buys_users"]]


def pull_stories_batch(story_ids: List[str]) -> pd.DataFrame:
    """One query for all unique story_ids across the whole backfill range."""
    if not story_ids:
        return pd.DataFrame(columns=["story_id","title_hn","title_en","author","category_hn","url","is_premium"])
    ids_sql = "','".join(story_ids)
    sql = f"""
    SELECT
      story_id, title_hn, title,
      author_firstname_hn, author_lastname_hn,
      category_title_hn, full_slug, is_premium
    FROM stories
    WHERE story_id IN ('{ids_sql}')
    LIMIT 100000
    """
    df = pinot_query(sql)
    if df.empty:
        return pd.DataFrame(columns=["story_id","title_hn","title_en","author","category_hn","url","is_premium"])
    df["author"] = (df["author_firstname_hn"].fillna("") + " " + df["author_lastname_hn"].fillna("")).str.strip()
    df["url"] = URL_PREFIX + df["full_slug"].fillna("")
    out = df[["story_id","title_hn","title","author","category_title_hn","url","is_premium"]].rename(
        columns={"title":"title_en","category_title_hn":"category_hn"}
    )
    return out.drop_duplicates(subset=["story_id"], keep="first")


# ---------- Google Sheets ----------
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

def _open_ws(tab: str, cols: int):
    sh = _client().open_by_key(SHEET_ID)
    try:
        return sh.worksheet(tab)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(tab, rows=1000, cols=cols)


def upsert_dated_bulk(tab: str, new_df: pd.DataFrame, cols: List[str], dates_touched: set):
    """
    Read existing ONCE, drop rows where date IN dates_touched, concat with new, write ONCE.
    """
    ws = _open_ws(tab, len(cols))
    log(f"  [{tab}] reading existing ...")
    try:
        existing_raw = ws.get_all_records()
    except Exception as e:
        log(f"  [{tab}] read failed ({e}) — treating as empty")
        existing_raw = []
    existing = pd.DataFrame(existing_raw)

    if not existing.empty and "date" in existing.columns and dates_touched:
        existing = existing[~existing["date"].astype(str).isin({str(d) for d in dates_touched})]

    combined = pd.concat(
        [existing[cols] if not existing.empty else pd.DataFrame(columns=cols),
         new_df[cols] if not new_df.empty else pd.DataFrame(columns=cols)],
        ignore_index=True,
    )
    log(f"  [{tab}] writing {len(combined)} total rows ...")
    _write(ws, combined, cols)


def upsert_dim_bulk(tab: str, new_df: pd.DataFrame, key: str, cols: List[str]):
    ws = _open_ws(tab, len(cols))
    log(f"  [{tab}] reading existing ...")
    try:
        existing_raw = ws.get_all_records()
    except Exception as e:
        log(f"  [{tab}] read failed ({e}) — treating as empty")
        existing_raw = []
    existing = pd.DataFrame(existing_raw)
    if not existing.empty and key in existing.columns and not new_df.empty:
        new_keys = set(new_df[key].astype(str).unique())
        existing = existing[~existing[key].astype(str).isin(new_keys)]
    combined = pd.concat(
        [existing[cols] if not existing.empty else pd.DataFrame(columns=cols),
         new_df[cols] if not new_df.empty else pd.DataFrame(columns=cols)],
        ignore_index=True,
    )
    log(f"  [{tab}] writing {len(combined)} total rows ...")
    _write(ws, combined, cols)


def _write(ws, df: pd.DataFrame, cols: List[str]):
    ws.clear()
    if df.empty:
        ws.update([cols], value_input_option="RAW")
        return
    data = [cols] + df.astype(str).fillna("").values.tolist()
    ws.update(data, value_input_option="RAW")


# ---------- orchestration ----------
FEED_COLS = ["date","hour","feed_version_id","position","story_id","fi_users","fc_users","fi_events","fc_events"]
PW_COLS   = ["date","hour","story_id","pv_users","pc_users","vil_users","bc_users","buys_users"]
DIM_COLS  = ["story_id","title_hn","title_en","author","category_hn","url","is_premium"]


def main():
    args = sys.argv[1:]
    # filter out empties (Actions passes empty strings when inputs blank)
    args = [a for a in args if a.strip()]
    if not args:
        ist_now = dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)
        start = end = (ist_now - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    elif len(args) == 1:
        start = end = args[0]
    else:
        start, end = args[0], args[1]

    d0 = dt.date.fromisoformat(start)
    d1 = dt.date.fromisoformat(end)
    if d1 < d0: d0, d1 = d1, d0

    log(f"range: {d0} .. {d1}  (host={PINOT_HOST}, sheet={SHEET_ID[:8]}…)")

    # accumulate in memory
    feed_frames = []
    pw_frames = []
    dates_touched = set()
    all_story_ids = set()

    cur = d0
    while cur <= d1:
        date_str = cur.strftime("%Y-%m-%d")
        dates_touched.add(date_str)

        log(f"[{date_str}] fetching feed metrics ...")
        try:
            feed_df = pull_feed_metrics(date_str)
        except Exception as e:
            log(f"[{date_str}] feed query failed: {e}")
            feed_df = pd.DataFrame()

        if not feed_df.empty:
            feed_frames.append(feed_df)
            day_stories = set(feed_df["story_id"].astype(str).unique())
            all_story_ids |= day_stories
            log(f"[{date_str}] feed rows: {len(feed_df)} / unique stories this day: {len(day_stories)}")

            log(f"[{date_str}] fetching paywall metrics (single query) ...")
            try:
                pw_df = pull_paywall_metrics_day(date_str, day_stories)
                if not pw_df.empty:
                    pw_frames.append(pw_df)
                log(f"[{date_str}] paywall rows kept: {len(pw_df)}")
            except Exception as e:
                log(f"[{date_str}] paywall query failed: {e}")
        else:
            log(f"[{date_str}] no feed rows — skipping paywall")

        cur += dt.timedelta(days=1)

    # consolidate
    feed_all = pd.concat(feed_frames, ignore_index=True) if feed_frames else pd.DataFrame(columns=FEED_COLS)
    pw_all   = pd.concat(pw_frames, ignore_index=True)   if pw_frames   else pd.DataFrame(columns=PW_COLS)

    log(f"\nTOTAL: feed={len(feed_all)}  paywall={len(pw_all)}  stories={len(all_story_ids)}")

    log("fetching story dim (one batch query) ...")
    dim_all = pull_stories_batch(sorted(all_story_ids))
    log(f"dim rows: {len(dim_all)}")

    # Filter to premium stories only — positions 5/13/21/27/28 contain non-premium
    # stories too (personalized feed), but dashboard is for hard-paywall analysis.
    if not dim_all.empty and "is_premium" in dim_all.columns:
        premium_ids = set(
            dim_all[dim_all["is_premium"].astype(str).str.lower() == "true"]["story_id"].astype(str)
        )
        before_feed, before_pw = len(feed_all), len(pw_all)
        feed_all = feed_all[feed_all["story_id"].astype(str).isin(premium_ids)].copy()
        pw_all   = pw_all[pw_all["story_id"].astype(str).isin(premium_ids)].copy()
        dim_all  = dim_all[dim_all["story_id"].astype(str).isin(premium_ids)].copy()
        log(f"premium filter: feed {before_feed}→{len(feed_all)}, paywall {before_pw}→{len(pw_all)}, dim kept {len(dim_all)}")

    log("\nwriting to Google Sheet ...")
    upsert_dated_bulk("feed_metrics",    feed_all.reindex(columns=FEED_COLS), FEED_COLS, dates_touched)
    upsert_dated_bulk("paywall_metrics", pw_all.reindex(columns=PW_COLS),     PW_COLS,   dates_touched)
    upsert_dim_bulk("stories_dim", dim_all, key="story_id", cols=DIM_COLS)

    log("\n✓ ALL DONE.")


if __name__ == "__main__":
    main()
