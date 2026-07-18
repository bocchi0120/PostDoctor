"""分析層: 曜日×時間帯の集計とヒートマップ生成。

storage 層が返す DataFrame のみを入力とし、DB には触れない
（完全ローカル・API消費ゼロ）。
"""

from __future__ import annotations

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from postdoctor.config import Account

WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
METRICS = ["impressions", "engagement", "engagement_rate", "likes"]

for _font in ["Yu Gothic", "Meiryo", "MS Gothic", "Noto Sans CJK JP"]:
    try:
        matplotlib.rcParams["font.family"] = _font
        break
    except Exception:
        continue


def add_derived_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        raise ValueError("投稿データが空です。先に fetch を実行してください。")
    df = df.copy()
    df["engagement"] = df["likes"] + df["retweets"] + df["replies"] + df["quotes"] + df["bookmarks"]
    df["engagement_rate"] = df.apply(
        lambda r: r["engagement"] / r["impressions"] * 100 if r["impressions"] else 0.0,
        axis=1,
    )
    return df


def pivot_table(df: pd.DataFrame, metric: str) -> pd.DataFrame:
    return df.pivot_table(
        index="weekday", columns="hour", values=metric, aggfunc="mean"
    ).reindex(index=range(7), columns=range(24))


def make_heatmap(df: pd.DataFrame, metric: str, account: Account) -> str:
    pivot = pivot_table(df, metric)

    fig, ax = plt.subplots(figsize=(14, 5))
    im = ax.imshow(pivot.values, aspect="auto", cmap="YlOrRd")
    ax.set_xticks(range(24), labels=[f"{h}時" for h in range(24)], fontsize=8)
    ax.set_yticks(range(7), labels=WEEKDAYS)
    ax.set_title(f"曜日×時間帯 平均{metric}（@{account.screen_name}）")
    fig.colorbar(im, ax=ax, shrink=0.8)

    for i in range(7):
        for j in range(24):
            v = pivot.values[i][j]
            if pd.notna(v):
                ax.text(j, i, f"{v:,.0f}" if metric == "impressions" else f"{v:.1f}",
                        ha="center", va="center", fontsize=6)

    out = account.data_dir / f"heatmap_{metric}.png"
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return str(out)


def best_slots(df: pd.DataFrame, metric: str, top_n: int = 5, min_count: int = 2) -> pd.DataFrame:
    return (
        df.groupby(["weekday", "hour"])[metric]
        .agg(["mean", "count"])
        .reset_index()
        .query("count >= @min_count")
        .sort_values("mean", ascending=False)
        .head(top_n)
    )
