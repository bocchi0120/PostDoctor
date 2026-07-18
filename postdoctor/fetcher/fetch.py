"""取得層: X API から投稿＋メトリクスを差分取得する。

従量課金対策として since_id による差分取得のみを行い、同じ投稿を
二度読まない。DB へのアクセスは一切行わず、正規化した PostRecord の
リストを返すだけ（永続化は storage 層の責務）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta, timezone

from postdoctor.config import Account
from postdoctor.fetcher.x_client import build_client
from postdoctor.storage.db import PostRecord

JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class FetchResult:
    posts: list[PostRecord]
    newest_id: str | None


def fetch_new_posts(account: Account, since_id: str | None) -> FetchResult:
    client = build_client(account)

    kwargs = dict(
        id=account.user_id,
        max_results=100,
        tweet_fields=["created_at", "public_metrics", "text"],
        exclude=["retweets", "replies"],
        user_auth=True,
    )
    if since_id:
        kwargs["since_id"] = since_id

    resp = client.get_users_tweets(**kwargs)

    if not resp.data:
        return FetchResult(posts=[], newest_id=None)

    posts: list[PostRecord] = []
    for t in resp.data:
        created_jst = t.created_at.astimezone(JST)
        m = t.public_metrics or {}
        posts.append(
            PostRecord(
                id=str(t.id),
                created_at=created_jst.isoformat(),
                weekday=created_jst.weekday(),
                hour=created_jst.hour,
                text=t.text,
                impressions=m.get("impression_count", 0),
                likes=m.get("like_count", 0),
                retweets=m.get("retweet_count", 0),
                replies=m.get("reply_count", 0),
                quotes=m.get("quote_count", 0),
                bookmarks=m.get("bookmark_count", 0),
            )
        )

    newest_id = resp.meta.get("newest_id")
    return FetchResult(posts=posts, newest_id=newest_id)
