"""Date-grouped purged walk-forward splits."""

from __future__ import annotations

import pandas as pd


def purged_time_folds(
    events: pd.DataFrame,
    *,
    date_col: str = "date",
    end_col: str = "exit_date",
    embargo: int = 10,
) -> list[dict]:
    """Expanding folds: 36m train / 6m calib / 6m test, roll 6m.

    Events whose [signal, exit] interval overlaps the next segment are purged.
    Dates must be ISO strings or timestamps.
    """
    df = events.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    if end_col in df.columns:
        df[end_col] = pd.to_datetime(df[end_col])
    else:
        df[end_col] = df[date_col] + pd.Timedelta(days=embargo)
    start = df[date_col].min()
    stop = df[date_col].max()
    folds = []
    train_end = start + pd.DateOffset(months=36)
    while True:
        calib_end = train_end + pd.DateOffset(months=6)
        test_end = calib_end + pd.DateOffset(months=6)
        if test_end > stop + pd.Timedelta(days=1):
            break
        train = df[df[date_col] < train_end]
        calib = df[(df[date_col] >= train_end) & (df[date_col] < calib_end)]
        test = df[(df[date_col] >= calib_end) & (df[date_col] < test_end)]
        # Purge training events whose exit crosses into calib
        train = train[train[end_col] < train_end - pd.Timedelta(days=embargo)]
        calib = calib[calib[end_col] < calib_end - pd.Timedelta(days=embargo)]
        folds.append(
            {
                "train_end": str(pd.Timestamp(train_end).date()),
                "calib_end": str(pd.Timestamp(calib_end).date()),
                "test_end": str(pd.Timestamp(test_end).date()),
                "n_train": int(len(train)),
                "n_calib": int(len(calib)),
                "n_test": int(len(test)),
            }
        )
        train_end = train_end + pd.DateOffset(months=6)
        if train_end > stop:
            break
    return folds
