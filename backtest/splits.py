"""Date-grouped purged walk-forward splits on exchange sessions."""

from __future__ import annotations

import pandas as pd


def session_map(sessions) -> dict[pd.Timestamp, int]:
    out = {}
    for i, session in enumerate(sessions):
        out[pd.Timestamp(session).tz_localize(None).normalize()] = i
    return out


def fold_windows(start, end, *, train_months: int = 36, calib_months: int = 6, test_months: int = 6) -> list[dict]:
    start_ts = pd.Timestamp(start).normalize()
    end_ts = pd.Timestamp(end).normalize()
    windows = []
    train_end = start_ts + pd.DateOffset(months=train_months)
    while True:
        calib_end = train_end + pd.DateOffset(months=calib_months)
        test_end = calib_end + pd.DateOffset(months=test_months)
        if test_end > end_ts + pd.Timedelta(days=1):
            break
        windows.append(
            {
                "train_start": str(start_ts.date()),
                "train_end": str(train_end.date()),
                "calib_end": str(calib_end.date()),
                "test_end": str(test_end.date()),
            }
        )
        train_end = train_end + pd.DateOffset(months=6)
        if len(windows) > 40:
            break
    return windows


def _session_index(smap: dict, ts) -> int | None:
    if pd.isna(ts):
        return None
    day = pd.Timestamp(ts).tz_localize(None).normalize()
    if day in smap:
        return smap[day]
    prior = [d for d in smap if d <= day]
    if not prior:
        return None
    return smap[max(prior)]


def split_fold(events: pd.DataFrame, window: dict, smap: dict, *, embargo_sessions: int = 10) -> dict[str, pd.DataFrame]:
    df = events.copy()
    df["date"] = pd.to_datetime(df["date"])
    df["exit_date"] = pd.to_datetime(df["exit_date"])
    if "status" in df.columns:
        missing = df[(df["status"] == "filled") & df["exit_date"].isna()]
        if len(missing):
            raise ValueError("missing exit timestamps")
    train_start = pd.Timestamp(window["train_start"])
    train_end = pd.Timestamp(window["train_end"])
    calib_end = pd.Timestamp(window["calib_end"])
    test_end = pd.Timestamp(window["test_end"])
    train = df[(df["date"] >= train_start) & (df["date"] < train_end)]
    calib = df[(df["date"] >= train_end) & (df["date"] < calib_end)]
    test = df[(df["date"] >= calib_end) & (df["date"] < test_end)]

    def purge(part: pd.DataFrame, boundary) -> pd.DataFrame:
        boundary_i = _session_index(smap, boundary)
        if boundary_i is None:
            return part.iloc[0:0].copy()
        keep = []
        for idx, row in part.iterrows():
            exit_i = _session_index(smap, row["exit_date"])
            if exit_i is None:
                continue
            if exit_i >= boundary_i - int(embargo_sessions):
                continue
            keep.append(idx)
        return part.loc[keep].reset_index(drop=True)

    return {
        "train": purge(train, train_end),
        "calib": purge(calib, calib_end),
        "test": test.reset_index(drop=True),
    }


def purged_time_folds(events: pd.DataFrame, *, date_col: str = "date", end_col: str = "exit_date", embargo: int = 10) -> list[dict]:
    """Compatibility summary of the session-purged folds."""
    df = events.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    sessions = list(pd.bdate_range(df[date_col].min(), df[date_col].max()))
    smap = session_map(sessions)
    windows = fold_windows(df[date_col].min(), df[date_col].max())
    if end_col not in df.columns:
        df[end_col] = pd.NaT
    df["exit_date"] = df[end_col]
    df["date"] = df[date_col]
    df["status"] = df.get("status", "filled")
    out = []
    for window in windows:
        parts = split_fold(df, window, smap, embargo_sessions=embargo)
        out.append({**window, "n_train": int(len(parts["train"])), "n_calib": int(len(parts["calib"])), "n_test": int(len(parts["test"]))})
    return out
