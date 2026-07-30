"""取得層: X API v2 recent search でリプライ候補となる投稿を収集する。

従量課金対策として:
  - config/keywords.json の daily_read_limit（デフォルト50件/日）を超えたら打ち切る
  - 既にDBにある投稿IDは二度読まない（呼び出し側が db.known_ids() で除外）
  - フォロワー数取得（$0.010/件）は上位候補のみに限定する

DB へのアクセスは行わず、正規化した Candidate のリストを返すだけ
（永続化は db 層の責務）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta, timezone

from postdoctor.config import ROOT_DIR, Account
from postdoctor.fetcher.x_client import build_client
from postdoctor.reply_scout import db

JST = timezone(timedelta(hours=9))
KEYWORDS_PATH = ROOT_DIR / "config" / "keywords.json"
NG_WORDS_PATH = ROOT_DIR / "config" / "ng_words.json"
ANALYSIS_TERMS_PATH = ROOT_DIR / "config" / "analysis_terms.json"

TWEET_READ_COST = 0.005
USER_READ_COST = 0.010


@dataclass(frozen=True)
class ScoutConfig:
    keywords: list[str]
    specific_terms: list[str]
    ng_words: list[str]
    solicitation_words: list[str]
    analysis_terms: list[str]
    trusted_authors: list[str]
    daily_read_limit: int
    min_likes: int
    min_replies: int
    top_user_lookup_limit: int
    claude_model: str
    draft_top_n: int
    rakuba_output_dir: str


def _load_word_list(path, key: str) -> list[str]:
    """ng_words.json/analysis_terms.jsonのような補助的な語彙リストを読む。

    欠損・壊れていてもスコアリング自体は止めたくないので空リストに縮退する
    （keywords.json自体が無い場合とは異なり、こちらは無くても動作継続できる）。
    """
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return raw.get(key, [])


def load_config() -> ScoutConfig:
    if not KEYWORDS_PATH.exists():
        raise RuntimeError(f"キーワード設定ファイルが見つかりません: {KEYWORDS_PATH}")
    raw = json.loads(KEYWORDS_PATH.read_text(encoding="utf-8"))
    return ScoutConfig(
        keywords=raw.get("keywords", []),
        specific_terms=raw.get("specific_terms", []),
        ng_words=_load_word_list(NG_WORDS_PATH, "ng_words"),
        solicitation_words=_load_word_list(NG_WORDS_PATH, "solicitation_words"),
        analysis_terms=_load_word_list(ANALYSIS_TERMS_PATH, "analysis_terms"),
        trusted_authors=raw.get("trusted_authors", []),
        daily_read_limit=raw.get("daily_read_limit", 50),
        min_likes=raw.get("min_likes", 5),
        min_replies=raw.get("min_replies", 0),
        top_user_lookup_limit=raw.get("top_user_lookup_limit", 15),
        claude_model=raw.get("claude_model", "claude-sonnet-5"),
        draft_top_n=raw.get("draft_top_n", 5),
        rakuba_output_dir=raw.get("rakuba_output_dir", ""),
    )


def collect_candidates(
    account: Account, conn, cfg: ScoutConfig
) -> tuple[list[db.Candidate], int]:
    """キーワード検索で新規候補を集める。

    戻り値: (新規候補のリスト, 今回APIから読み取ったツイート数)
    読み取り数は日次上限（cfg.daily_read_limit から本日分を差し引いた残数）を超えない。
    """
    already_read_today = db.get_today_usage(conn, "tweet_read")
    remaining = max(cfg.daily_read_limit - already_read_today, 0)
    if remaining <= 0 or not cfg.keywords:
        return [], 0

    client = build_client(account)
    collected: list[db.Candidate] = []
    read_count = 0

    for keyword in cfg.keywords:
        if read_count >= remaining:
            break
        query = f"{keyword} -is:retweet -is:reply lang:ja"
        resp = client.search_recent_tweets(
            query=query,
            max_results=min(100, max(10, remaining - read_count)),
            tweet_fields=["created_at", "public_metrics", "text", "author_id"],
            expansions=["author_id"],
            user_fields=["username"],
            user_auth=True,
        )
        if not resp.data:
            continue

        read_count += len(resp.data)
        users_by_id = {u.id: u for u in (resp.includes or {}).get("users", [])}
        ids = [str(t.id) for t in resp.data]
        known = db.known_ids(conn, ids)

        for t in resp.data:
            if str(t.id) in known:
                continue
            m = t.public_metrics or {}
            likes = m.get("like_count", 0)
            replies_n = m.get("reply_count", 0)
            if likes < cfg.min_likes or replies_n < cfg.min_replies:
                continue
            user = users_by_id.get(t.author_id)
            screen_name = user.username if user else str(t.author_id)
            created_jst = t.created_at.astimezone(JST)
            collected.append(
                db.Candidate(
                    id=str(t.id),
                    text=t.text,
                    author_id=str(t.author_id),
                    author_screen_name=screen_name,
                    created_at=created_jst.isoformat(),
                    likes=likes,
                    retweets=m.get("retweet_count", 0),
                    replies=replies_n,
                    quotes=m.get("quote_count", 0),
                    keyword=keyword,
                )
            )

    return collected, read_count


def enrich_followers(account: Account, author_ids: list[str]) -> dict[str, int]:
    """上位候補の投稿者フォロワー数を取得する（$0.010/件なので呼び出し側で件数を絞ること）。"""
    if not author_ids:
        return {}
    client = build_client(account)
    resp = client.get_users(ids=author_ids, user_fields=["public_metrics"], user_auth=True)
    result: dict[str, int] = {}
    for u in resp.data or []:
        followers = (u.public_metrics or {}).get("followers_count", 0)
        result[str(u.id)] = followers
    return result


def fetch_reply_responses(
    account: Account, targets: list[db.TrackTarget], max_read: int | None = None
) -> tuple[list[db.ReplyResponse], int]:
    """送信済みリプライへの返信を取得し、author_idで自分/他者に分類する（他者のみ返す）。

    scoutで収集する候補は "-is:reply" のトップレベル投稿のみなので、
    candidate.id はその会話のconversation_idと一致する。これを利用して
    候補（conversation_id）単位で会話全体を1回のsearch_recent_tweetsで取得し、
    referenced_tweets(replied_to)がこのグループ内の送信済みリプライIDを指す
    ツイートだけを「返信」として抽出する（会話に含まれる無関係な他者→元投稿者の
    やり取りを誤検知しないため）。

    max_read: 今回読み取ってよいツイート数の上限（呼び出し側がcfg.daily_read_limitと
    本日のtweet_read使用量から残数を計算して渡す想定。scoutの候補収集と同じ
    daily_read_limit予算を共有する）。Noneの場合は上限なし（テスト等での明示的な
    無制限指定用途）。

    戻り値: (他者からの返信のリスト, 今回APIから読み取ったツイート数)
    """
    if not targets or (max_read is not None and max_read <= 0):
        return [], 0

    by_candidate: dict[str, list[str]] = {}
    for t in targets:
        by_candidate.setdefault(t.candidate_id, []).append(t.reply_id)

    client = build_client(account)
    responses: list[db.ReplyResponse] = []
    read_count = 0

    for candidate_id, reply_ids in by_candidate.items():
        if max_read is not None and read_count >= max_read:
            break
        reply_id_set = set(reply_ids)
        query_max_results = 100 if max_read is None else min(100, max(10, max_read - read_count))
        resp = client.search_recent_tweets(
            query=f"conversation_id:{candidate_id}",
            max_results=query_max_results,
            tweet_fields=["created_at", "author_id", "referenced_tweets", "text"],
            expansions=["author_id"],
            user_fields=["username"],
            user_auth=True,
        )
        if not resp.data:
            continue
        read_count += len(resp.data)
        users_by_id = {u.id: u for u in (resp.includes or {}).get("users", [])}

        for t in resp.data:
            parent_id = next(
                (str(ref.id) for ref in (t.referenced_tweets or []) if ref.type == "replied_to"),
                None,
            )
            if parent_id not in reply_id_set:
                continue
            if str(t.author_id) == account.user_id:
                continue  # 自分自身の返信（答え合わせリプ等）は対象外
            user = users_by_id.get(t.author_id)
            created_jst = t.created_at.astimezone(JST)
            responses.append(
                db.ReplyResponse(
                    id=str(t.id),
                    parent_reply_id=parent_id,
                    author_id=str(t.author_id),
                    author_username=user.username if user else None,
                    text=t.text,
                    created_at=created_jst.isoformat(),
                )
            )

    return responses, read_count


def fetch_own_reply_metrics(account: Account, reply_ids: list[str]) -> dict[str, dict]:
    """送信済みリプライ（自分の投稿）の反応を取得する。自分の投稿の読み取りなので$0.001/件。"""
    if not reply_ids:
        return {}
    client = build_client(account)
    resp = client.get_tweets(ids=reply_ids, tweet_fields=["public_metrics"], user_auth=True)
    result: dict[str, dict] = {}
    for t in resp.data or []:
        m = t.public_metrics or {}
        result[str(t.id)] = {
            "impressions": m.get("impression_count", 0),
            "likes": m.get("like_count", 0),
            "retweets": m.get("retweet_count", 0),
            "replies": m.get("reply_count", 0),
        }
    return result
