"""予測データ層: Rakubaプロジェクト（C:\\Projects\\Rakuba）の実際の予測出力を読む。

reply_scoutの他の層（fetcher/db/analyzer/prescriber）と並ぶ層として、Rakubaの
生データ形式（predictions.json・predictions_YYYYMMDD.json・race_results.csv）を
知っているのはこのファイルだけにする。

出力ディレクトリが存在しない・読めない・壊れている場合は、例外を投げず空の結果
（空リスト/空集合/None）に縮退する。reply_scoutはRakubaの出力に依存するが、
Rakuba側の都合（未起動・パス未設定・ファイル未生成）で reply_scout 全体が
落ちることは避ける。

`mark_for_score()`のスコア区分・結果表記（除外時の扱い等）は
C:\\Projects\\Rakuba\\ops\\x_post.py の _full_mark()/build_result_draft() と
同じ規約に合わせている（Rakuba自身の投稿と評価軸の見た目を揃えるため）。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

SNAPSHOT_RE = re.compile(r"predictions_(\d{8})\.json")
ROLLING_FILENAME = "predictions.json"
RESULTS_FILENAME = "race_results.csv"

MARKS = ["◎", "〇", "▲"]
FACT_CHARS_RE = re.compile(r"[0-9◎〇△×▲]")


@dataclass(frozen=True)
class PredictionConfig:
    output_dir: Path
    rolling_filename: str = ROLLING_FILENAME
    results_filename: str = RESULTS_FILENAME


def load_prediction_config(rakuba_output_dir: str) -> PredictionConfig:
    return PredictionConfig(output_dir=Path(rakuba_output_dir) if rakuba_output_dir else Path())


@dataclass(frozen=True)
class HorseRow:
    race_key: str
    kaisai_date: str
    jyo_name: str
    race_num: str
    race_name: str
    distance: int
    pred_rank: int
    uma_num: int
    uma_code: str
    uma_name: str
    score: float
    # 出走頭数。uma_num>0(馬番確定)の行だけを数える。レース内に1頭でもuma_num==0
    # (枠順未確定のプレースホルダ)が混ざっていれば頭数を確定できないのでNone
    # (確定済みの馬だけ数えると実際より少ない頭数を断定してしまうため)。
    field_size: int | None = None


def _race_label(horse: HorseRow) -> str:
    try:
        race_num = str(int(horse.race_num))
    except (TypeError, ValueError):
        race_num = str(horse.race_num)
    base = f"{horse.jyo_name}{race_num}R"
    return f"{base} {horse.race_name}" if horse.race_name else base


def _field_size(horses: list[dict]) -> int | None:
    uma_nums = [int(h.get("uma_num") or 0) for h in horses]
    if not uma_nums or any(n == 0 for n in uma_nums):
        return None
    return len(uma_nums)


def _horses_from_race(race: dict) -> list[HorseRow]:
    rows = []
    horses = race.get("horses", [])
    field_size = _field_size(horses)
    for h in horses:
        rows.append(
            HorseRow(
                race_key=str(race.get("race_key", "")),
                kaisai_date=str(race.get("kaisai_date", "")),
                jyo_name=str(race.get("jyo_name", "")),
                race_num=str(race.get("race_num", "")),
                race_name=str(race.get("race_name", "") or ""),
                distance=int(race.get("distance") or 0),
                pred_rank=int(h.get("pred_rank") or 0),
                uma_num=int(h.get("uma_num") or 0),
                uma_code=str(h.get("uma_code", "")),
                uma_name=str(h.get("uma_name", "")),
                score=float(h.get("score") or 0.0),
                field_size=field_size,
            )
        )
    return rows


def _load_prediction_file(path: Path) -> list[HorseRow]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = []
    for race in raw.get("races", []):
        rows.extend(_horses_from_race(race))
    return rows


def load_all_predictions(pcfg: PredictionConfig) -> list[HorseRow]:
    """rolling predictions.json + predictions_YYYYMMDD.json スナップショットを統合する。

    (race_key, uma_code) で重複排除し、rolling ファイルの内容を優先する
    （スナップショットは過去バックアップなので、同じキーがあれば最新のrollingが勝つ）。
    出力ディレクトリが無い/空でもエラーにせず空リストを返す。
    """
    if not pcfg.output_dir or not pcfg.output_dir.is_dir():
        return []

    merged: dict[tuple[str, str], HorseRow] = {}

    snapshot_paths = sorted(
        p for p in pcfg.output_dir.glob("predictions_*.json") if SNAPSHOT_RE.fullmatch(p.name)
    )
    for path in snapshot_paths:
        for row in _load_prediction_file(path):
            merged[(row.race_key, row.uma_code)] = row

    rolling_path = pcfg.output_dir / pcfg.rolling_filename
    if rolling_path.exists():
        for row in _load_prediction_file(rolling_path):
            merged[(row.race_key, row.uma_code)] = row

    return list(merged.values())


def compute_predictions_signature(pcfg: PredictionConfig) -> str | None:
    """rolling + snapshotファイル群の内容から決定的なハッシュを計算する。

    下書き生成時にこの値をdrafts.predictions_hashへ保存しておき、表示・
    送信済み操作の各タイミングで再計算した値と突き合わせる。枠順確定等で
    predictions.jsonが更新された後に、古い評価を引用したままの下書きが
    残留する（公式投稿と矛盾するリスク）ことを検知するためのもの。
    出力ディレクトリが無い場合はNone（比較不能=警告なし扱い）。
    """
    if not pcfg.output_dir or not pcfg.output_dir.is_dir():
        return None

    paths = sorted(
        p for p in pcfg.output_dir.glob("predictions_*.json") if SNAPSHOT_RE.fullmatch(p.name)
    )
    rolling_path = pcfg.output_dir / pcfg.rolling_filename
    if rolling_path.exists():
        paths.append(rolling_path)

    digest = hashlib.sha256()
    for path in paths:
        try:
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        except OSError:
            continue
    return digest.hexdigest()


def load_race_results(pcfg: PredictionConfig) -> dict[str, dict[str, dict]]:
    """race_key -> uma_code -> {"chakujun": int, "uma_num": int}

    race_results.csv を csv.DictReader で1回ストリーム読みする
    （x_post.pyの_load_race_resultsと同じ方式。pandasは使わない）。
    """
    results_path = pcfg.output_dir / pcfg.results_filename
    result: dict[str, dict[str, dict]] = {}
    if not results_path.exists():
        return result
    with open(results_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            race_key = row.get("race_key", "")
            uma_code = row.get("uma_code", "")
            if not race_key or not uma_code:
                continue
            result.setdefault(race_key, {})[uma_code] = {
                "chakujun": int(row.get("chakujun") or 0),
                "uma_num": int(row.get("uma_num") or 0),
            }
    return result


def confirmed_race_keys(results: dict[str, dict[str, dict]]) -> set[str]:
    """race_results.csvで、chakujun>0の行が1件以上あるrace_keyの集合（レース自体が確定済みか）。

    以前は行の存在自体で確定済みと判定していたが、枠順未確定(uma_num=0)の
    プレースホルダ行がレース確定前にrace_results.csvへ先行して書き込まれる
    ケースがあり、未来のレース（例: 関屋記念）への返信下書きに誤って
    「(結果除外)」が付く実害が発生した。行の存在ではなく、chakujun>0の行が
    実際に1件以上あることをもって「レースが行われ結果が出た」とみなす
    （全頭が本当に出走除外だった極めて稀なケースでは結果節を付けなくなるが、
    誤って未来のレースに結果を捏造するよりは安全側）。個々の馬が実際に
    完走したか除外だったかは find_match() 側で chakujun の値を見て判断する。
    """
    return {
        race_key
        for race_key, horses in results.items()
        if any(info["chakujun"] > 0 for info in horses.values())
    }


def mark_for_score(score: float) -> str:
    """x_post.pyの_full_mark()と同一基準。"""
    if score >= 0.9:
        return "◎◎"
    if score >= 0.6:
        return "◎"
    if score >= 0.3:
        return "〇"
    if score >= -0.2:
        return "△"
    return "×"


def all_horse_names(rows: list[HorseRow]) -> set[str]:
    """既知の馬名集合。analyzer.pyの考察系ボーナス判定（馬名+分析語の共起）に使う。"""
    return {r.uma_name for r in rows if r.uma_name}


def all_race_names(rows: list[HorseRow]) -> set[str]:
    """既知のレース名集合。analyzer.pyの具体性判定（馬名またはレース名の名指し）に使う。"""
    return {r.race_name for r in rows if r.race_name}


# 結果カテゴリ: 評価の高低×実際の好走/凡走で4分類し、fact_sentenceとClaudeへの
# トーン指示の両方をこれに応じて分岐させる（外れも含めて誠実に開示する方針のため、
# 隠したり弱めたりはしない。トーンだけをカテゴリに応じて変える）。
CATEGORY_HIT = "的中系"            # 高評価 かつ 好走(3着以内)
CATEGORY_MISS_HIGH = "見込み違い系"  # 高評価 だが 凡走
CATEGORY_MISS_LOW = "完敗系"        # 低評価 だが 好走（評価を覆された）
CATEGORY_AS_EXPECTED = "順当系"     # 低評価 かつ 凡走（評価通り）

HIGH_EVAL_MAX_PRED_RANK = 3     # 3番手評価以内を「高評価」とみなす
GOOD_RESULT_MAX_CHAKUJUN = 3     # 3着以内を「好走」とみなす


@dataclass(frozen=True)
class PredictionMatch:
    horse: HorseRow
    race_label: str
    mark: str
    concluded: bool
    chakujun: int | None  # None=未確定, 0=除外
    fact_sentence: str
    category: str | None  # 的中系/見込み違い系/完敗系/順当系、除外・未確定時はNone


def _classify(pred_rank: int, chakujun: int) -> str:
    # 高評価/低評価は事実文に出す「N番手評価」と同じ基準(レース内順位)で判定する。
    # 以前はscore閾値で判定しており、スコアの低い本命が2着だと「評価を大きく
    # 上回りました」になる矛盾があった(docs/design_decisions.md 4節11項)。
    high_eval = 1 <= pred_rank <= HIGH_EVAL_MAX_PRED_RANK
    good_result = 1 <= chakujun <= GOOD_RESULT_MAX_CHAKUJUN
    if high_eval and good_result:
        return CATEGORY_HIT
    if high_eval and not good_result:
        return CATEGORY_MISS_HIGH
    if not high_eval and good_result:
        return CATEGORY_MISS_LOW
    return CATEGORY_AS_EXPECTED


def _build_fact_sentence(
    horse: HorseRow, mark: str, chakujun: int | None
) -> tuple[str, str | None]:
    """fact_sentenceと、Claudeへのトーン指示に使うcategoryを返す。

    順位・頭数・マーク・着順は全て実データそのまま（外れの引用も隠さない）。
    テンプレート文言だけがカテゴリごとに異なる。生スコア(0.25等)は受け手に
    意味が伝わらない内部値なので文面には出さない(_classify()の判定にも使わない)。

    AI判定(◎◎/◎/〇/△/×, mark_for_score())と、馬個体の相対評価(pred_rank=
    出走馬内でのN番手)は別概念であり、混同しない（Rakuba ops/x_post.pyの
    MARK_NOTE「AI判定は◎(本命)自体の自信度」の通り、AI判定はそのレースの
    本命馬1頭のスコアだけから決まるレース単位の指標で、2番手以下の馬には
    そもそも定義されない。以前、find_match()が対象馬がどのpred_rankであっても
    一律mark_for_score(その馬自身のscore)を「◎評価」のように個体評価として
    文面化しており、例えば2番手評価の馬を「△評価」と書いてしまうなど、
    「AI判定」と「その馬の順位評価」を混同した実例があった。詳細は
    docs/design_decisions.md参照）。

    - pred_rank==1(本命/◎馬): 「本命(1番手評価)」と表記し、そのレースの
      AI判定(=本命自体の自信度)を別文で添える。
    - pred_rank>=2(◎以外): 「N番手評価」とだけ表記する。AI判定マークは
      本命にしか紐づかない概念なので、ここでは一切使わない。
    - 頭数が確定できる場合は「18頭中3番手評価」のように頭数を添える
      (HorseRow.field_size参照。確定できなければ「3番手評価」のみ)。
    """
    label = _race_label(horse)
    field = (
        f"{horse.field_size}頭中"
        if horse.field_size and horse.pred_rank <= horse.field_size
        else ""
    )
    if horse.pred_rank == 1:
        core = f"本命({field}1番手評価)"
    elif horse.pred_rank >= 2:
        core = f"{field}{horse.pred_rank}番手評価"
    else:
        # pred_rankが未設定/0などの異常値(本来のRakuba出力では発生しない想定)。
        # 順位を断定表示できないので、旧来通りAI判定マークをそのまま流用する
        # フォールバックに留める。
        core = f"{mark}評価"
    prefix = f"うちのAIモデルは{label}で『{horse.uma_name}』を{core}"

    if chakujun is None:
        sentence, category = prefix + "としています。", None
    elif chakujun == 0:
        sentence, category = prefix + "としていました(結果除外)。", None
    else:
        category = _classify(horse.pred_rank, chakujun)
        if category == CATEGORY_HIT:
            sentence = prefix + f"としていて、実際に{chakujun}着と好走しました。"
        elif category == CATEGORY_MISS_HIGH:
            sentence = prefix + f"としていましたが、結果は{chakujun}着にとどまりました。"
        elif category == CATEGORY_MISS_LOW:
            sentence = prefix + f"としていましたが、結果は{chakujun}着と、評価を大きく上回りました。"
        else:  # CATEGORY_AS_EXPECTED
            sentence = prefix + f"としていて、結果も{chakujun}着と評価通りでした。"

    if horse.pred_rank == 1:
        # AI判定は本命自体の自信度というレース単位の情報なので、個体評価の文とは
        # 分けて別文で添える(混同防止)。
        sentence += f"(このレースのAI判定は{mark})"

    return sentence, category


# レース確定後に「前哨戦分析」「予想」等のレース前フレーミングの投稿へ返信すると、
# 時制が噛み合わない（過去の予想投稿に結果を突きつける形になる）ため、該当する
# 場合はマッチ自体をスキップする。
PRE_RACE_INDICATORS = [
    "前哨戦", "予想", "本命候補", "参戦", "全頭診断", "全頭短評",
    "1週前", "週前", "見どころ", "注目馬", "診断",
]


def _is_pre_race_framing(text: str) -> bool:
    return any(term in text for term in PRE_RACE_INDICATORS)


SHORT_NAME_THRESHOLD = 3  # この文字数以下の馬名は単独一致だけでは採用しない（誤マッチ対策）


def _horse_name_matches(h: HorseRow, candidate_text: str) -> bool:
    """馬名が本文に含まれるかを判定する。

    短い馬名（3文字以下）は競馬に無関係な文脈でも偶然一致しやすいため、
    レース名または開催場名との共起も要求する（例:「一」のような極端な例は
    そもそもuma_nameとして現実的でないが、2〜3文字の馬名は実在するので
    安全側に倒す）。
    """
    if h.uma_name not in candidate_text:
        return False
    if len(h.uma_name) > SHORT_NAME_THRESHOLD:
        return True
    context_ok = (h.race_name and h.race_name in candidate_text) or (
        h.jyo_name and h.jyo_name in candidate_text
    )
    return bool(context_ok)


# find_match()がマッチしなかった理由。呼び出し側(prescriber.py)がDBに保存する
# prediction_status文言を出し分けるために使う（「本当に一致が無い」のか
# 「一致はしたが時制ミスマッチで見送った」のかを区別するため）。
REASON_NO_MATCH = "no_match"
REASON_TIME_MISMATCH = "time_mismatch"


def _parse_kaisai_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _parse_created_at_date(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value).date()
    except (ValueError, TypeError):
        return None


def _select_candidate(
    horse_candidates: list[HorseRow], candidate_text: str, created_at: str | None
) -> HorseRow:
    """同名馬が複数レースの予測データにまたがってヒットした場合に1件へ絞り込む。

    実例: 「ウェイワードアクト」が7/16マリーンS・8/9エルムSの両方に出走登録されており、
    本文が明らかにマリーンSの話をしている投稿に対し、単純に開催日が新しい方
    （エルムS）を機械的に選んでしまい無関係な事実文を生成した（2026-08-09）。
    以降は本文中の手がかりを優先し、開催日の新しさは最終フォールバックに格下げする。

    1. 本文にレース名または開催場名が明記されているものがあれば、それに絞る。
    2. 手がかりが無ければ、投稿日時（created_at）に開催日が最も近いものを選ぶ
       （「新しいレースほど話題にされやすい」ではなく、「投稿時点に近いレースの方が
       その投稿の指すレースである可能性が高い」という前提の方が実態に合うため）。
    3. created_atが無い/パース不能な場合のみ、開催日が最も新しいものを選ぶ
       （従来の挙動。手がかりが一切無いときの最終フォールバック）。
    """
    if len(horse_candidates) == 1:
        return horse_candidates[0]

    named = [
        h
        for h in horse_candidates
        if (h.race_name and h.race_name in candidate_text)
        or (h.jyo_name and h.jyo_name in candidate_text)
    ]
    pool = named if named else horse_candidates
    if len(pool) == 1:
        return pool[0]

    post_date = _parse_created_at_date(created_at) if created_at else None
    if post_date is not None:
        dated = [(h, _parse_kaisai_date(h.kaisai_date)) for h in pool]
        dated = [(h, d) for h, d in dated if d is not None]
        if dated:
            return min(dated, key=lambda pair: abs((pair[1] - post_date).days))[0]

    return max(pool, key=lambda h: h.kaisai_date)


def find_match(
    candidate_text: str,
    horses: list[HorseRow],
    results: dict[str, dict[str, dict]],
    confirmed: set[str],
    created_at: str | None = None,
) -> tuple[PredictionMatch | None, str | None]:
    """candidate_textに実在の馬名が含まれるかを調べ、あれば事実文を確定させる。

    馬名一致のみを採用する（レース名だけの一致からの「本命採用」フォールバックは
    廃止 — 投稿者が言及していない馬をこちらの本命として引用すると、投稿の文脈と
    無関係な引用になりがちなため）。

    戻り値は (PredictionMatch|None, 不一致理由|None) のタプル。マッチした場合は
    (match, None)。しなかった場合は (None, REASON_NO_MATCH | REASON_TIME_MISMATCH)。

    1. 既知の馬名（2文字以上）が本文に部分一致するものを探す。3文字以下の
       短い馬名は誤マッチしやすいため、レース名または開催場名との共起も必須とする。
       同名馬が複数レースにまたがってヒットした場合は _select_candidate() で
       本文のレース名/開催場名 → 投稿日時への近さ → 開催日の新しさ、の順に絞り込む。
    2. 一致する馬名が無ければ (None, REASON_NO_MATCH)（下書き生成をハードスキップ
       する合図）。
    3. 一致したレースが確定済みで、かつ投稿がレース前フレーミング（「前哨戦分析」
       「予想」等）である場合は (None, REASON_TIME_MISMATCH) を返す（レース前の
       投稿に確定済み結果を突きつける時制ミスマッチを避けるため）。
       ※このキーワード判定は投稿全体を見るため、過去の話と別の未来レースの話が
       混在する投稿では過検知になり得る（既知の制約。実例が増えたら馬名/レース名
       の近傍判定への改善を検討する）。
    """
    horse_candidates = [
        h for h in horses if len(h.uma_name) >= 2 and _horse_name_matches(h, candidate_text)
    ]
    if not horse_candidates:
        return None, REASON_NO_MATCH
    chosen = _select_candidate(horse_candidates, candidate_text, created_at)

    mark = mark_for_score(chosen.score)
    concluded = chosen.race_key in confirmed

    if concluded and _is_pre_race_framing(candidate_text):
        return None, REASON_TIME_MISMATCH

    chakujun: int | None = None
    if concluded:
        info = results.get(chosen.race_key, {}).get(chosen.uma_code)
        chakujun = info["chakujun"] if info else 0

    fact_sentence, category = _build_fact_sentence(chosen, mark, chakujun)
    match = PredictionMatch(
        horse=chosen,
        race_label=_race_label(chosen),
        mark=mark,
        concluded=concluded,
        chakujun=chakujun,
        fact_sentence=fact_sentence,
        category=category,
    )
    return match, None
