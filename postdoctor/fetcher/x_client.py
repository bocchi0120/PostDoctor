"""X (Twitter) API クライアント生成。"""

from __future__ import annotations

import tweepy

from postdoctor.config import Account


def build_client(account: Account) -> tweepy.Client:
    creds = account.credentials()
    return tweepy.Client(
        consumer_key=creds.consumer_key,
        consumer_secret=creds.consumer_secret,
        access_token=creds.access_token,
        access_token_secret=creds.access_token_secret,
    )
