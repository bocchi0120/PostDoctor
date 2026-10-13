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
from dataclasses import dataclass, field
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
    # 直近の重賞名をscoutの検索キーワードに自動で加える（orchestrator.run_scout()）
    auto_graded_races: bool = False
    graded_grades: tuple[str, ...] = ("A", "B", "C")
    max_graded_races: int = 4
    # 正式名称(または通称) -> 検索に使う表記のリスト。複数ならOR検索1回にまとめる
    race_aliases: dict[str, list[str]] = field(default_factory=dict)
    rakuba_cancelled_races_path: str = ""


MIN_RESULTS_PER_QUERY = 10   # X API v2 recent search の max_results 下限
MAX_RESULTS_PER_QUERY = 100  # 同 上限


def _normalize_aliases(raw) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    aliases = {}
    for name, value in raw.items():
        variants = [value] if isinstance(value, str) else list(value or [])
        variants = [v for v in variants if isinstance(v, str) and v]
        if variants:
            aliases[str(name)] = variants
    return aliases


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
    auto = raw.get("auto_graded_races") or {}
    return ScoutConfig(
        keywords=raw.get("keywords", []),
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
        auto_graded_races=bool(auto.get("enabled", False)),
        graded_grades=tuple(auto.get("grades", ["A", "B", "C"])),
        max_graded_races=int(auto.get("max_races", 4)),
        race_aliases=_normalize_aliases(raw.get("race_aliases", {})),
        rakuba_cancelled_races_path=raw.get("rakuba_cancelled_races_path", ""),
    )


def _query_term(variants: tuple[str, ...]) -> str:
    """表記の組を1つの検索語にする。X検索は「〜S」と「〜ステークス」を同一視しないためOR。"""
    if len(variants) == 1:
        return variants[0]
    return "(" + " OR ".join(variants) + ")"


def collect_candidates(
    account: Account,
    conn,
    cfg: ScoutConfig,
    keyword_groups: list[tuple[str, ...]] | None = None,
    stats: list[db.SearchStat] | None = None,
) -> tuple[list[db.Candidate], int, list[tuple[str, int]]]:
    """キーワード検索で新規候補を集める。

    keyword_groups: 検索する表記の組の優先順リスト（1組=1回の検索）。省略時は
    cfg.keywords を1語ずつ使う。上限を超える組は後ろから切り捨てる。

    戻り値: (新規候補のリスト, 今回APIから読み取ったツイート数,
             実際に検索した (キーワード, 読み取り数) のリスト)
    読み取り数は日次上限（cfg.daily_read_limit から本日分を差し引いた残数）を超えない。
    残数はキーワードごとに均等に割り振る（先頭の1語で残数を使い切って後ろの語が一度も
    検索されない、という2026-09-08〜10-05に起きていた状態を防ぐ）。max_resultsの下限が
    10なので検索回数は「残数÷10」回まで。結果が割り当てに満たなかった分は後ろの語に回す。

    stats: リストを渡すと、検索1回ごとの内訳（読み取り・保存・落とした理由別の件数）を
    追記する。記録用で、検索や候補の選別には影響しない。
    """
    already_read_today = db.get_today_usage(conn, "tweet_read")
    remaining = max(cfg.daily_read_limit - already_read_today, 0)
    groups = keyword_groups if keyword_groups is not None else [(k,) for k in cfg.keywords]
    if remaining <= 0 or not groups:
        return [], 0, []
    groups = groups[: max(remaining // MIN_RESULTS_PER_QUERY, 1)]

    client = build_client(account)
    collected: list[db.Candidate] = []
    read_count = 0
    searched: list[tuple[str, int]] = []
    collected_ids: set[str] = set()

    for i, variants in enumerate(groups):
        left = remaining - read_count
        if left <= 0:
            break
        per_query = min(
            MAX_RESULTS_PER_QUERY, max(MIN_RESULTS_PER_QUERY, left // (len(groups) - i))
        )
        keyword = variants[0]
        query = f"{_query_term(variants)} -is:retweet -is:reply lang:ja"
        resp = client.search_recent_tweets(
            query=query,
            max_results=per_query,
            tweet_fields=[
                "created_at", "public_metrics", "text", "author_id", "referenced_tweets",
            ],
            expansions=["author_id"],
            user_fields=["username"],
            user_auth=True,
        )
        searched.append((keyword, len(resp.data or [])))
        dropped_known = dropped_low_engagement = dropped_duplicate = 0
        if not resp.data:
            if stats is not None:
                stats.append(db.SearchStat(keyword, query, per_query, 0, 0, 0, 0, 0))
            continue

        read_count += len(resp.data)
        users_by_id = {u.id: u for u in (resp.includes or {}).get("users", [])}
        ids = [str(t.id) for t in resp.data]
        known = db.known_ids(conn, ids)

        for t in resp.data:
            if str(t.id) in known:
                dropped_known += 1
                continue
            m = t.public_metrics or {}
            likes = m.get("like_count", 0)
            replies_n = m.get("reply_count", 0)
            if likes < cfg.min_likes or replies_n < cfg.min_replies:
                dropped_low_engagement += 1
                continue
            # 同じ実行の前の語で拾った投稿は、従来通りcollectedには入れる
            # (db.insert_candidates()のON CONFLICTで無視される)。内訳の上でだけ区別する。
            if str(t.id) in collected_ids:
                dropped_duplicate += 1
            collected_ids.add(str(t.id))
            user = users_by_id.get(t.author_id)
            screen_name = user.username if user else str(t.author_id)
            created_jst = t.created_at.astimezone(JST)
            is_quote = any(ref.type == "quoted" for ref in (t.referenced_tweets or []))
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
                    is_quote=is_quote,
                )
            )

        if stats is not None:
            dropped = dropped_known + dropped_low_engagement + dropped_duplicate
            stats.append(
                db.SearchStat(
                    keyword, query, per_query, len(resp.data), len(resp.data) - dropped,
                    dropped_known, dropped_low_engagement, dropped_duplicate,
                )
            )

    return collected, read_count, searched


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
