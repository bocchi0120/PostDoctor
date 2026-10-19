"""予測データ層: Rakubaプロジェクト（C:\\Projects\\Rakuba）の実際の予測出力を読む。

reply_scoutの他の層（fetcher/db/analyzer/prescriber）と並ぶ層として、Rakubaの
生データ形式（predictions.json・predictions_YYYYMMDD.json・race_results.csv・race_info.csv）を
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
import unicodedata
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SNAPSHOT_RE = re.compile(r"predictions_(\d{8})\.json")
ROLLING_FILENAME = "predictions.json"
RESULTS_FILENAME = "race_results.csv"
RACE_INFO_FILENAME = "race_info.csv"
JST = timezone(timedelta(hours=9))

MARKS = ["◎", "〇", "▲"]
FACT_CHARS_RE = re.compile(r"[0-9◎〇△×▲]")


@dataclass(frozen=True)
class PredictionConfig:
    output_dir: Path
    rolling_filename: str = ROLLING_FILENAME
    results_filename: str = RESULTS_FILENAME
    race_info_filename: str = RACE_INFO_FILENAME


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
    # 発走時刻 "HHMM"（JST）。predictions.jsonのrace.start_time。古いスナップショットには
    # 無いので、load_all_predictions()がrace_info.csvで補う。分からなければ""。
    start_time: str = ""


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
                start_time=str(race.get("start_time") or ""),
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

    rows = list(merged.values())
    if any(not r.start_time for r in rows):
        start_times = load_race_start_times(pcfg)
        rows = [
            replace(r, start_time=start_times.get(r.race_key, "")) if not r.start_time else r
            for r in rows
        ]
    return rows


def load_race_start_times(pcfg: PredictionConfig) -> dict[str, str]:
    """race_info.csvの race_key -> start_time("HHMM")。古いスナップショットには発走時刻が
    無いので、その補完に使う。文字列のまま読む（"0955"の先頭ゼロを壊さないため）。"""
    path = pcfg.output_dir / pcfg.race_info_filename
    if not path.exists():
        return {}
    start_times: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                race_key = row.get("race_key", "")
                start_time = (row.get("start_time") or "").strip()
                if race_key and start_time:
                    start_times[race_key] = start_time
    except (OSError, csv.Error):
        return {}
    return start_times


def race_start_at(horse: HorseRow) -> datetime | None:
    """kaisai_date＋start_timeを発走日時(JST)にする。どちらかが欠けている・読めなければNone。"""
    if len(horse.start_time) != 4 or not horse.start_time.isdigit():
        return None
    try:
        return datetime.strptime(horse.kaisai_date + horse.start_time, "%Y%m%d%H%M").replace(
            tzinfo=JST
        )
    except ValueError:
        return None


def has_started(horse: HorseRow, now: datetime) -> bool:
    """nowの時点で発走時刻を過ぎているか。発走日時が分からないレースはFalse（判定しない）。
    nowにタイムゾーンが無ければJSTとみなす。"""
    start = race_start_at(horse)
    if start is None:
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=JST)
    return now >= start


def posted_before_start(horse: HorseRow, created_at: str | None) -> bool:
    """投稿日時(ISO8601)が発走時刻より前か。発走日時・投稿日時のどちらかが分からなければ
    False（判定しない）。投稿日時にタイムゾーンが無ければJSTとみなす。"""
    start = race_start_at(horse)
    if start is None or not created_at:
        return False
    try:
        posted = datetime.fromisoformat(created_at)
    except (ValueError, TypeError):
        return False
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=JST)
    return posted < start


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


# 結果カテゴリ: 評価の高低×実際の好走/凡走で分類し、fact_sentenceとClaudeへの
# トーン指示の両方をこれに応じて分岐させる（外れも含めて誠実に開示する方針のため、
# 隠したり弱めたりはしない。トーンだけをカテゴリに応じて変える）。
# 評価は3帯: 高評価(3番手以内) / 上位評価(4番手〜上位3分の1) / 低評価(それ以下)。
CATEGORY_HIT = "的中系"            # 高評価 かつ 好走(3着以内)
CATEGORY_MISS_HIGH = "見込み違い系"  # 高評価・上位評価 だが 凡走（着順が評価順位より下）
CATEGORY_MISS_LOW = "完敗系"        # 低評価 だが 好走（評価を覆された）
CATEGORY_AS_EXPECTED = "順当系"     # 低評価で着順が評価順位と同じか下、または上位評価で着順が評価順位以内（評価通り）
CATEGORY_NEUTRAL = "中立系"        # 上位評価 かつ 好走、または低評価で好走でないが着順が評価順位より上

HIGH_EVAL_MAX_PRED_RANK = 3     # 3番手評価以内を「高評価」とみなす
GOOD_RESULT_MAX_CHAKUJUN = 3     # 3着以内を「好走」とみなす
UPPER_EVAL_FIELD_DIVISOR = 3     # 頭数の上位3分の1までを「上位評価」とみなす


def upper_eval_max_pred_rank(field_size: int | None) -> int:
    """「上位評価」とみなす評価順位の上限: max(3, ceil(頭数/3))。

    18頭→6番手、16頭→6番手、14頭→5番手、9頭以下→3番手。頭数不明(None)は3
    （=高評価の閾値と同じになり、上位評価の帯は無くなる）。
    """
    if not field_size:
        return HIGH_EVAL_MAX_PRED_RANK
    return max(HIGH_EVAL_MAX_PRED_RANK, -(-field_size // UPPER_EVAL_FIELD_DIVISOR))


@dataclass(frozen=True)
class PredictionMatch:
    horse: HorseRow
    race_label: str
    mark: str
    concluded: bool
    chakujun: int | None  # None=未確定, 0=除外
    fact_sentence: str
    category: str | None  # 的中系/見込み違い系/完敗系/順当系/中立系、除外・未確定時はNone


def _classify(pred_rank: int, chakujun: int, field_size: int | None = None) -> str:
    # 高評価/低評価は事実文に出す「N番手評価」と同じ基準(レース内順位)で判定する。
    # 以前はscore閾値で判定しており、スコアの低い本命が2着だと「評価を大きく
    # 上回りました」になる矛盾があった(docs/design_decisions.md 4節11項)。
    #
    # 4番手〜上位3分の1(「上位評価」)は、以前は頭数によらず低評価扱いで、18頭中6番手の
    # 馬が17着でも「評価通り」になっていた(同15項)。この帯は次のように扱う:
    # - 凡走で着順が評価順位より下 → 見込み違い系
    # - 凡走でも着順が評価順位以内(14頭中5番手→5着) → 順当系(評価通り)
    # - 好走 → 中立系。的中系にはしない(5番手評価の馬の勝利を的中のように語ると
    #   水増しになる)。完敗系にもしない(上位に見ていた馬なので「大きく上回った」は言い過ぎ)。
    #
    # 低評価の帯で好走(3着以内)でない場合、以前は着順を見ずに一律「評価通り」で、
    # 16頭中14番手評価→4着のように評価を上回った結果まで「評価通り」と書いていた
    # (2026-10-16)。着順が評価順位より上なら中立系、同じか下なら順当系にする。
    high_eval = 1 <= pred_rank <= HIGH_EVAL_MAX_PRED_RANK
    upper_eval = HIGH_EVAL_MAX_PRED_RANK < pred_rank <= upper_eval_max_pred_rank(field_size)
    good_result = 1 <= chakujun <= GOOD_RESULT_MAX_CHAKUJUN
    if high_eval:
        return CATEGORY_HIT if good_result else CATEGORY_MISS_HIGH
    if upper_eval:
        if good_result:
            return CATEGORY_NEUTRAL
        return CATEGORY_MISS_HIGH if chakujun > pred_rank else CATEGORY_AS_EXPECTED
    if good_result:
        return CATEGORY_MISS_LOW
    return CATEGORY_NEUTRAL if chakujun < pred_rank else CATEGORY_AS_EXPECTED


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
        category = _classify(horse.pred_rank, chakujun, horse.field_size)
        if category == CATEGORY_HIT:
            sentence = prefix + f"としていて、実際に{chakujun}着と好走しました。"
        elif category == CATEGORY_NEUTRAL:
            sentence = prefix + f"としていて、結果は{chakujun}着でした。"
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

# カタカナ(長音「ー」・半角カナを含む)。馬名の直前/直後がこれなら別の単語の一部とみなす。
_KATAKANA_RE = re.compile(r"[\u30A0-\u30FF\uFF66-\uFF9F]")


def _name_at_word_boundary(name: str, text: str) -> bool:
    """nameがtext中に、前後をカタカナに挟まれない形で1回以上現れるか。

    単純な部分文字列一致だと「ジオ」が「ラジオNIKKEI」に、「クラン」が別馬の
    「クランフォード」に一致してしまう（2026-09-28、保存済み候補の短い馬名一致の
    大半がこの種の誤マッチだった）。馬名はカタカナ表記なので、前後がカタカナなら
    より長いカタカナ語の一部と判断して不一致にする。全馬名に適用する。
    """
    for m in re.finditer(re.escape(name), text):
        before = text[m.start() - 1] if m.start() > 0 else ""
        after = text[m.end()] if m.end() < len(text) else ""
        if not _KATAKANA_RE.match(before) and not _KATAKANA_RE.match(after):
            return True
    return False


def _core_race_name(race_name: str) -> str:
    """「農林水産省賞典　小倉記念」→「小倉記念」。

    Rakubaのrace_nameは冠名付きの正式名称のことがあり、投稿では通称(全角スペース
    以降)で書かれるため、共起判定には通称側を使う。
    """
    return race_name.split("\u3000")[-1].strip() if race_name else ""


def _race_specific_context(h: HorseRow, candidate_text: str) -> bool:
    """本文にそのレースを特定できる手がかりがあるか（短い馬名用の文脈条件）。

    - race_nameが空欄のレースはFalse（文脈条件を満たせないので短い馬名は一致させない。
      安全側に倒す）。
    - レース名(通称)が本文にあればTrue。
    - 開催場名+レース番号(「函館9R」「函館 9R」「函館9レース」等)があればTrue。
    - 開催場名だけではFalse。「函館」は「函館記念」のように別レースの名前に含まれ、
      同じ開催場の全レースに共通するため、レースを特定する手がかりにならない
      （2026-09-28、「函館記念…サインもらえた」が函館9R北斗特別の馬『サイン』に
      誤マッチした実例）。
    """
    core = _core_race_name(h.race_name)
    if not core:
        return False
    if core in candidate_text:
        return True
    try:
        race_num = int(h.race_num)
    except (TypeError, ValueError):
        return False
    if not h.jyo_name:
        return False
    pattern = rf"{re.escape(h.jyo_name)}\s*{race_num}\s*(?:R|Ｒ|レース)"
    return re.search(pattern, candidate_text) is not None


def _horse_name_matches(h: HorseRow, candidate_text: str) -> bool:
    """馬名が本文に含まれるかを判定する。

    1. 全馬名: 一致部分の前後がカタカナなら不一致（_name_at_word_boundary()）。
    2. 短い馬名（3文字以下）: 一般名詞(「サイン」等)と偶然一致しやすいため、
       そのレースを特定できる手がかりの共起も要求する（_race_specific_context()）。
    """
    if not _name_at_word_boundary(h.uma_name, candidate_text):
        return False
    if len(h.uma_name) > SHORT_NAME_THRESHOLD:
        return True
    return _race_specific_context(h, candidate_text)


# find_match()がマッチしなかった理由。呼び出し側(prescriber.py)がDBに保存する
# prediction_status文言を出し分けるために使う（「本当に一致が無い」のか
# 「一致はしたが時制ミスマッチで見送った」のかを区別するため）。
REASON_NO_MATCH = "no_match"
REASON_TIME_MISMATCH = "time_mismatch"
# 発走済みだが結果がまだ取り込まれていない（土日のレースは月曜昼の一括配信まで確定しない）。
# 「…評価としています」とレース前の言い方で返すとレース後の投稿と食い違うので見送る。
REASON_RESULT_PENDING = "result_pending"


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
    now: datetime | None = None,
) -> tuple[PredictionMatch | None, str | None]:
    """candidate_textに実在の馬名が含まれるかを調べ、あれば事実文を確定させる。

    馬名一致のみを採用する（レース名だけの一致からの「本命採用」フォールバックは
    廃止 — 投稿者が言及していない馬をこちらの本命として引用すると、投稿の文脈と
    無関係な引用になりがちなため）。

    戻り値は (PredictionMatch|None, 不一致理由|None) のタプル。マッチした場合は
    (match, None)。しなかった場合は
    (None, REASON_NO_MATCH | REASON_TIME_MISMATCH | REASON_RESULT_PENDING)。

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
    4. nowを渡した場合、一致したレースが未確定で、now時点で発走時刻を過ぎていれば
       (None, REASON_RESULT_PENDING) を返す（結果の取り込み待ち。比べるのは投稿日時では
       なく現在時刻: レース前の投稿でも、発走後に「…評価としています」と返すのは不自然）。
       発走時刻が分からないレースはこの判定をしない。nowを省略した場合も判定しない。
    5. 投稿日時(created_at)が発走時刻より前で、そのレースが発走済み（確定済み、または
       now時点で発走時刻を過ぎている）なら、結果の有無を問わず (None, REASON_TIME_MISMATCH)
       を返す。3の語による判定を通り抜けるレース前の投稿（「明日勝つためのおまじない」
       「秋華賞はモンローウォークが本命」など。2026-10-18秋華賞の実例）を、発走時刻で止める。
       発走時刻が分からないレース、created_atが無い/読めない場合はこの判定をしない。
    """
    horse_candidates = [
        h for h in horses if len(h.uma_name) >= 2 and _horse_name_matches(h, candidate_text)
    ]
    if not horse_candidates:
        return None, REASON_NO_MATCH
    # 長い馬名の一致がある場合は、短い馬名の一致を候補から外す。_select_candidate()は
    # 同名馬の別レース判別用で一致の強さを見ないため、放置すると弱い一致(短い馬名)が
    # 開催日の近さだけで強い一致に勝ってしまう（2026-09-28、本文の話題
    # 『ファウストラーゼン』ではなく『サイン』が選ばれた実例）。
    long_matches = [h for h in horse_candidates if len(h.uma_name) > SHORT_NAME_THRESHOLD]
    if long_matches:
        horse_candidates = long_matches
    chosen = _select_candidate(horse_candidates, candidate_text, created_at)

    mark = mark_for_score(chosen.score)
    concluded = chosen.race_key in confirmed

    if concluded and _is_pre_race_framing(candidate_text):
        return None, REASON_TIME_MISMATCH
    started = concluded or (now is not None and has_started(chosen, now))
    if started and posted_before_start(chosen, created_at):
        return None, REASON_TIME_MISMATCH
    if not concluded and now is not None and has_started(chosen, now):
        return None, REASON_RESULT_PENDING

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


# --- scoutの検索キーワード用: 直近の重賞レース名 ---------------------------------

GRADED_CODES = ("A", "B", "C")  # A=G1, B=G2, C=G3（L=リステッド, E=特別は対象外）
_SATURDAY = 5
_TUESDAY = 1
_STAKES_SUFFIX = "ステークス"


@dataclass(frozen=True)
class GradedRace:
    race_key: str
    kaisai_date: str
    jyo_name: str
    race_num: str
    race_name: str
    grade_code: str


def graded_race_window(today: date) -> tuple[date, date]:
    """検索対象にする開催日の範囲: 今日より前の直近の土曜 〜 今日以降の最初の火曜。

    月〜木は前週末の重賞（月曜14時以降は結果も確定）、金〜日は前週末＋当週末の重賞が
    入る。週末の終わりを火曜にするのは、月曜(祝日)開催の中止が火曜に代替開催されうるため
    （C:\\Projects\\CLAUDE.md「土〜火」の週の窓）。
    """
    start = today - timedelta(days=(today.weekday() - _SATURDAY) % 7 or 7)
    end = today + timedelta(days=(_TUESDAY - today.weekday()) % 7)
    return start, end


def current_weekend_start(today: date) -> date:
    """「当週末」の土曜。土〜火はその週末（開催中〜結果確定まで）、水〜金は次の週末。"""
    days_since_saturday = (today.weekday() - _SATURDAY) % 7  # 土=0 … 金=6
    if days_since_saturday <= 3:  # 土・日・月・火
        return today - timedelta(days=days_since_saturday)
    return today + timedelta(days=7 - days_since_saturday)


def _load_graded_races(pcfg: PredictionConfig, grades: tuple[str, ...]) -> list[GradedRace]:
    """全予測ファイルからグレード対象のレースをrace_key単位で集める（rollingを優先）。"""
    if not pcfg.output_dir or not pcfg.output_dir.is_dir():
        return []
    paths = sorted(
        p for p in pcfg.output_dir.glob("predictions_*.json") if SNAPSHOT_RE.fullmatch(p.name)
    )
    rolling_path = pcfg.output_dir / pcfg.rolling_filename
    if rolling_path.exists():
        paths.append(rolling_path)

    merged: dict[str, GradedRace] = {}
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for race in raw.get("races", []):
            grade = str(race.get("grade_code", "") or "")
            name = str(race.get("race_name", "") or "")
            if grade not in grades or not name:
                continue
            race_key = str(race.get("race_key", ""))
            merged[race_key] = GradedRace(
                race_key=race_key,
                kaisai_date=str(race.get("kaisai_date", "")),
                jyo_name=str(race.get("jyo_name", "")),
                race_num=str(race.get("race_num", "")),
                race_name=name,
                grade_code=grade,
            )
    return list(merged.values())


def _load_cancellations(path: Path | None) -> list[dict]:
    """Rakubaの backend/cancelled_races.json（手動記録）を読む。無い/壊れていれば除外なし。"""
    if path is None or not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


def _is_cancelled(race: GradedRace, cancellations: list[dict]) -> bool:
    """中止された開催日・場のレースか。順延先の新しい日付のキーは別レースとして残る。"""
    for c in cancellations:
        if str(c.get("kaisai_date", "")) != race.kaisai_date:
            continue
        if str(c.get("jyo_name", "")) != race.jyo_name:
            continue
        race_nums = c.get("race_nums")
        if race_nums is None:
            return True
        try:
            if int(race.race_num) in {int(n) for n in race_nums}:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _search_variants(race_name: str, aliases: dict[str, list[str]]) -> tuple[str, ...]:
    """検索に使う表記の組。X検索は「〜S」と「〜ステークス」を同一視しないため両方を持つ。

    race_aliasesに正式名称（または全角スペース以降の通称）があればその表記をそのまま使う。
    無ければ通称を使い、「〜ステークス」なら「〜S」も加える（投稿ではS表記が多い）。
    Rakubaの名称は全角英数字を含む（「ＣＢＣ賞」「中京２歳ステークス」）ので、自動で作る
    表記は投稿で一般的な半角にそろえる（NFKC）。
    """
    core = _core_race_name(race_name)
    for key in (race_name, core):
        if key in aliases and aliases[key]:
            return tuple(dict.fromkeys(aliases[key]))
    core = unicodedata.normalize("NFKC", core)
    if core.endswith(_STAKES_SUFFIX) and len(core) > len(_STAKES_SUFFIX):
        return (core[: -len(_STAKES_SUFFIX)] + "S", core)
    return (core,)


def graded_race_keywords(
    pcfg: PredictionConfig,
    today: date,
    *,
    cancelled_path: Path | None,
    aliases: dict[str, list[str]],
    grades: tuple[str, ...] = GRADED_CODES,
    max_races: int = 4,
) -> list[tuple[str, ...]]:
    """scoutで検索する直近の重賞レース名（表記の組）を優先順に返す。

    Rakubaが予測しているレースだけが対象（予測の無いレースの投稿を拾っても馬名一致が
    取れず下書きにならないため、外部の重賞日程は参照しない）。優先順は当週末→前週末、
    その中でG1→G2→G3、同グレード内は開催日が今日に近い順（金〜日は、前週末のG1より
    当週末のG2/G3を先にする）。中止日付のレースは除き、同じ表記は1つにまとめる。
    """
    return [
        variants
        for variants, _current in graded_race_keyword_plan(
            pcfg, today, cancelled_path=cancelled_path, aliases=aliases,
            grades=grades, max_races=max_races,
        )
    ]


def graded_race_keyword_plan(
    pcfg: PredictionConfig,
    today: date,
    *,
    cancelled_path: Path | None,
    aliases: dict[str, list[str]],
    grades: tuple[str, ...] = GRADED_CODES,
    max_races: int = 4,
) -> list[tuple[tuple[str, ...], bool]]:
    """graded_race_keywords()と同じ並びで、各表記の組が当週末のレースか(True)を添えて返す。
    同じ表記が当週末と前週末の両方にあれば、先に来る当週末の方として扱う。"""
    start, end = graded_race_window(today)
    cancellations = _load_cancellations(cancelled_path)
    in_window = []
    for race in _load_graded_races(pcfg, grades):
        race_date = _parse_kaisai_date(race.kaisai_date)
        if race_date is None or not (start <= race_date <= end):
            continue
        if _is_cancelled(race, cancellations):
            continue
        in_window.append((race, race_date))

    weekend_start = current_weekend_start(today)
    in_window.sort(key=lambda rd: (
        rd[1] < weekend_start,  # 前週末のレースは当週末の後ろ
        rd[0].grade_code,
        abs((rd[1] - today).days),
        rd[0].race_key,
    ))
    plan: list[tuple[tuple[str, ...], bool]] = []
    for race, race_date in in_window:
        variants = _search_variants(race.race_name, aliases)
        if all(variants != v for v, _current in plan):
            plan.append((variants, race_date >= weekend_start))
    return plan[:max_races]
