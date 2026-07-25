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
import json
import re
from dataclasses import dataclass
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


def _race_label(horse: HorseRow) -> str:
    try:
        race_num = str(int(horse.race_num))
    except (TypeError, ValueError):
        race_num = str(horse.race_num)
    base = f"{horse.jyo_name}{race_num}R"
    return f"{base} {horse.race_name}" if horse.race_name else base


def _horses_from_race(race: dict) -> list[HorseRow]:
    rows = []
    for h in race.get("horses", []):
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


# 結果カテゴリ: 評価の高低×実際の好走/凡走で4分類し、fact_sentenceとClaudeへの
# トーン指示の両方をこれに応じて分岐させる（外れも含めて誠実に開示する方針のため、
# 隠したり弱めたりはしない。トーンだけをカテゴリに応じて変える）。
CATEGORY_HIT = "的中系"            # 高評価 かつ 好走(3着以内)
CATEGORY_MISS_HIGH = "見込み違い系"  # 高評価 だが 凡走
CATEGORY_MISS_LOW = "完敗系"        # 低評価 だが 好走（評価を覆された）
CATEGORY_AS_EXPECTED = "順当系"     # 低評価 かつ 凡走（評価通り）

HIGH_EVAL_SCORE_THRESHOLD = 0.3  # mark_for_score()の〇/△境界と揃える
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


def _classify(score: float, chakujun: int) -> str:
    high_eval = score >= HIGH_EVAL_SCORE_THRESHOLD
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

    数値・マーク・着順は全て実データそのまま（外れの引用も隠さない）。
    テンプレート文言だけがカテゴリごとに異なる。
    """
    label = _race_label(horse)
    prefix = f"うちのAIモデルは{label}で『{horse.uma_name}』を{mark}評価(スコア{horse.score:.2f})"

    if chakujun is None:
        return prefix + "としています。", None
    if chakujun == 0:
        return prefix + "としていました(結果除外)。", None

    category = _classify(horse.score, chakujun)
    if category == CATEGORY_HIT:
        sentence = prefix + f"としていて、実際に{chakujun}着と好走しました。"
    elif category == CATEGORY_MISS_HIGH:
        sentence = prefix + f"と評価していましたが、結果は{chakujun}着にとどまりました。"
    elif category == CATEGORY_MISS_LOW:
        sentence = prefix + f"としていましたが、結果は{chakujun}着と、評価を大きく上回りました。"
    else:  # CATEGORY_AS_EXPECTED
        sentence = prefix + f"としていて、結果も{chakujun}着と評価通りでした。"
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


def find_match(
    candidate_text: str,
    horses: list[HorseRow],
    results: dict[str, dict[str, dict]],
    confirmed: set[str],
) -> tuple[PredictionMatch | None, str | None]:
    """candidate_textに実在の馬名が含まれるかを調べ、あれば事実文を確定させる。

    馬名一致のみを採用する（レース名だけの一致からの「本命採用」フォールバックは
    廃止 — 投稿者が言及していない馬をこちらの本命として引用すると、投稿の文脈と
    無関係な引用になりがちなため）。

    戻り値は (PredictionMatch|None, 不一致理由|None) のタプル。マッチした場合は
    (match, None)。しなかった場合は (None, REASON_NO_MATCH | REASON_TIME_MISMATCH)。

    1. 既知の馬名（2文字以上）が本文に部分一致するものを探す。3文字以下の
       短い馬名は誤マッチしやすいため、レース名または開催場名との共起も必須とする。
       複数該当する場合は開催日が新しいものを優先する。
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
    chosen = max(horse_candidates, key=lambda h: h.kaisai_date)

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
