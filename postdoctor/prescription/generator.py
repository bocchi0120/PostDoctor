"""処方箋生成層: 分析結果を「いつ・何を投稿すべきか」の提言に変換する。

analysis 層が返す集計結果のみを入力とし、DB や API には触れない。
出力は Markdown の「処方箋」として data/<account>/prescription.md に保存する。
`diagnose()` は構造化データを返すため、dashboard 層からも再利用できる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from postdoctor.analysis.heatmap import WEEKDAYS, best_slots
from postdoctor.config import Account

MIN_POSTS_FOR_CONFIDENCE = 10


@dataclass(frozen=True)
class Diagnosis:
    n: int
    avg_impressions: float
    avg_engagement_rate: float
    weekday_avg: float | None
    weekend_avg: float | None
    low_confidence: bool


def diagnose(df: pd.DataFrame) -> Diagnosis:
    weekend_mask = df["weekday"].isin([5, 6])
    weekday_avg = df.loc[~weekend_mask, "engagement_rate"].mean()
    weekend_avg = df.loc[weekend_mask, "engagement_rate"].mean()
    n = len(df)
    return Diagnosis(
        n=n,
        avg_impressions=df["impressions"].mean(),
        avg_engagement_rate=df["engagement_rate"].mean(),
        weekday_avg=weekday_avg if pd.notna(weekday_avg) else None,
        weekend_avg=weekend_avg if pd.notna(weekend_avg) else None,
        low_confidence=n < MIN_POSTS_FOR_CONFIDENCE,
    )


def _diagnosis_lines(diag: Diagnosis) -> list[str]:
    lines = [
        f"- 分析対象投稿数: {diag.n}件",
        f"- 平均インプレッション: {diag.avg_impressions:,.0f}",
        f"- 平均エンゲージメント率: {diag.avg_engagement_rate:.2f}%",
    ]
    if diag.weekday_avg is not None and diag.weekend_avg is not None:
        if diag.weekend_avg > diag.weekday_avg * 1.1:
            lines.append(
                f"- 週末（土日）の方が平日よりエンゲージメント率が高い傾向"
                f"（週末 {diag.weekend_avg:.2f}% vs 平日 {diag.weekday_avg:.2f}%）"
            )
        elif diag.weekday_avg > diag.weekend_avg * 1.1:
            lines.append(
                f"- 平日の方が週末よりエンゲージメント率が高い傾向"
                f"（平日 {diag.weekday_avg:.2f}% vs 週末 {diag.weekend_avg:.2f}%）"
            )
    if diag.low_confidence:
        lines.append(
            f"- ⚠️ サンプル数が少なく（{diag.n}件 < {MIN_POSTS_FOR_CONFIDENCE}件）、"
            "以下の処方は参考程度に留めてください。データが増え次第、再分析を推奨します。"
        )
    return lines


def _slot_lines(df: pd.DataFrame, metric: str, label: str, unit: str) -> list[str]:
    slots = best_slots(df, metric)
    if slots.empty:
        return [f"### {label}\n有効なデータ（同一枠2件以上）がまだありません。"]
    lines = [f"### {label}"]
    for _, r in slots.iterrows():
        lines.append(
            f"- {WEEKDAYS[int(r['weekday'])]}曜 {int(r['hour']):2d}時台: "
            f"平均 {r['mean']:,.1f}{unit}（{int(r['count'])}件）"
        )
    return lines


def generate_prescription(df: pd.DataFrame, account: Account) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    diag = diagnose(df)
    parts: list[str] = []
    parts.append(f"# 処方箋 - @{account.screen_name}")
    parts.append(f"作成日時: {now}\n")

    parts.append("## 診断")
    parts.extend(_diagnosis_lines(diag))
    parts.append("")

    parts.append("## 処方: おすすめ投稿枠")
    parts.extend(_slot_lines(df, "impressions", "インプレッション重視", ""))
    parts.append("")
    parts.extend(_slot_lines(df, "engagement_rate", "エンゲージメント率重視", "%"))
    parts.append("")

    parts.append("## 次回診察")
    parts.append(
        "- 週1回（月曜朝など）を目安に `postdoctor fetch` を再実行し、"
        "データを蓄積してから再度 `postdoctor prescribe` してください。"
    )

    text = "\n".join(parts)
    out_path = account.data_dir / "prescription.md"
    out_path.write_text(text, encoding="utf-8")
    return text
