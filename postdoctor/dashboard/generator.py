"""ダッシュボード生成: analysis層・prescription層の結果を1枚のHTMLに可視化する。

DB や API には触れず、analysis/prescription が返す DataFrame・集計結果のみを
消費する。`render_fragment()` は <style> + 本体のみ（<html>/<head>/<body> なし）
を返す断片で、`generate_dashboard()` はそれを完全なHTML文書として
data/<account>/dashboard.html に保存する。
"""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

import pandas as pd

from postdoctor.analysis.heatmap import METRICS, WEEKDAYS, best_slots, pivot_table
from postdoctor.config import Account
from postdoctor.dashboard.colors import (
    EMPTY_DARK,
    EMPTY_LIGHT,
    sequential_dark,
    sequential_light,
)
from postdoctor.prescription.generator import MIN_POSTS_FOR_CONFIDENCE, diagnose
from postdoctor.reply_scout import db as rs_db
from postdoctor.reply_scout import prediction_data as rs_prediction_data
from postdoctor.reply_scout.fetcher import load_config as rs_load_config

METRIC_LABELS = {
    "impressions": "インプレッション",
    "engagement": "エンゲージメント数",
    "engagement_rate": "エンゲージメント率",
    "likes": "いいね数",
}
METRIC_UNITS = {"impressions": "", "engagement": "", "engagement_rate": "%", "likes": ""}


def _fmt(metric: str, v: float) -> str:
    unit = METRIC_UNITS[metric]
    if unit == "%":
        return f"{v:.1f}%"
    return f"{v:,.0f}"


def _heatmap_card(df: pd.DataFrame, metric: str) -> str:
    values = pivot_table(df, metric)
    counts = df.pivot_table(
        index="weekday", columns="hour", values=metric, aggfunc="count"
    ).reindex(index=range(7), columns=range(24))

    present = values.values[pd.notna(values.values)]
    vmin = float(present.min()) if len(present) else 0.0
    vmax = float(present.max()) if len(present) else 1.0

    header_cells = "".join(
        f'<div class="hm-hour">{h}</div>' for h in range(24)
    )

    rows_html = []
    for wd in range(7):
        row_cells = [f'<div class="hm-weekday">{WEEKDAYS[wd]}</div>']
        for h in range(24):
            v = values.iat[wd, h]
            c = counts.iat[wd, h]
            if pd.isna(v):
                row_cells.append('<div class="cell cell-empty"></div>')
                continue
            t = 0.55 if vmax == vmin else (v - vmin) / (vmax - vmin)
            light = sequential_light(t)
            dark = sequential_dark(t)
            label = _fmt(metric, v)
            tip = f"{WEEKDAYS[wd]}曜 {h}時台: {label}（{int(c)}件）"
            row_cells.append(
                f'<div class="cell" style="--bg-light:{light};--bg-dark:{dark}" '
                f'title="{tip}" data-tip="{tip}">{label}</div>'
            )
        rows_html.append(f'<div class="hm-row">{"".join(row_cells)}</div>')

    legend = (
        '<div class="hm-legend">'
        f'<span>{_fmt(metric, vmin)}</span>'
        '<div class="hm-legend-bar"></div>'
        f'<span>{_fmt(metric, vmax)}</span>'
        "</div>"
        if len(present)
        else '<div class="hm-legend hm-legend-empty">データなし</div>'
    )

    return f"""
    <div class="card heatmap-card">
      <h3>{METRIC_LABELS[metric]}</h3>
      <div class="hm-scroll">
        <div class="hm-grid">
          <div class="hm-row hm-row-header">
            <div class="hm-weekday"></div>
            {header_cells}
          </div>
          {"".join(rows_html)}
        </div>
      </div>
      {legend}
    </div>
    """


def _slots_table(df: pd.DataFrame, metric: str, title: str) -> str:
    slots = best_slots(df, metric, top_n=5, min_count=1)
    if slots.empty:
        return f'<div class="card"><h3>{title}</h3><p class="muted">データがまだありません。</p></div>'

    rows = []
    for i, (_, r) in enumerate(slots.iterrows(), start=1):
        label = _fmt(metric, r["mean"])
        rows.append(
            f"<tr><td>{i}</td><td>{WEEKDAYS[int(r['weekday'])]}曜 {int(r['hour'])}時台</td>"
            f"<td class='num'>{label}</td><td class='num'>{int(r['count'])}件</td></tr>"
        )
    return f"""
    <div class="card">
      <h3>{title}</h3>
      <table class="slots-table">
        <thead><tr><th>順位</th><th>投稿枠</th><th>平均</th><th>件数</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </div>
    """


def _stat_tile(label: str, value: str) -> str:
    return f'<div class="stat-tile"><div class="stat-label">{label}</div><div class="stat-value">{value}</div></div>'


STYLE = """
<style>
.pd-dash {
  --surface-1: #fcfcfb; --surface-2: #f9f9f7; --text-primary: #0b0b0b;
  --text-secondary: #52514e; --text-muted: #898781; --gridline: #e1e0d9;
  --border: rgba(11,11,11,0.10); --warning: #fab219; --warning-ink: #6b4a05;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  color: var(--text-primary); background: var(--surface-2);
  padding: 24px; max-width: 1200px; margin: 0 auto;
}
@media (prefers-color-scheme: dark) {
  .pd-dash { --surface-1: #1a1a19; --surface-2: #0d0d0d; --text-primary: #ffffff;
    --text-secondary: #c3c2b7; --text-muted: #898781; --gridline: #2c2c2a;
    --border: rgba(255,255,255,0.10); --warning-ink: #ffe8b3; }
}
:root[data-theme="dark"] .pd-dash { --surface-1: #1a1a19; --surface-2: #0d0d0d; --text-primary: #ffffff;
  --text-secondary: #c3c2b7; --text-muted: #898781; --gridline: #2c2c2a;
  --border: rgba(255,255,255,0.10); --warning-ink: #ffe8b3; }
:root[data-theme="light"] .pd-dash { --surface-1: #fcfcfb; --surface-2: #f9f9f7; --text-primary: #0b0b0b;
  --text-secondary: #52514e; --text-muted: #898781; --gridline: #e1e0d9;
  --border: rgba(11,11,11,0.10); --warning-ink: #6b4a05; }

.pd-dash h1 { font-size: 22px; margin: 0 0 4px; }
.pd-dash h2 { font-size: 16px; margin: 32px 0 12px; color: var(--text-secondary); }
.pd-dash h3 { font-size: 14px; margin: 0 0 12px; color: var(--text-primary); }
.pd-dash .meta { color: var(--text-secondary); font-size: 13px; }
.pd-dash .muted { color: var(--text-muted); font-size: 13px; }

.banner {
  display: flex; align-items: center; gap: 8px; margin-top: 16px; padding: 10px 14px;
  border-radius: 8px; background: color-mix(in srgb, var(--warning) 18%, var(--surface-1));
  color: var(--warning-ink); font-size: 13px; border: 1px solid var(--warning);
}

.stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-top: 20px; }
.stat-tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.stat-label { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
.stat-value { font-size: 24px; font-weight: 600; }

.card-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }

.hm-scroll { overflow-x: auto; }
.hm-grid { display: flex; flex-direction: column; width: max-content; }
.hm-row { display: grid; grid-template-columns: 34px repeat(24, 26px); }
.hm-weekday { display: flex; align-items: center; justify-content: center; font-size: 11px; color: var(--text-secondary); }
.hm-hour { font-size: 8px; color: var(--text-muted); text-align: center; padding-bottom: 2px; }
.cell {
  position: relative; height: 22px; margin: 1px; border-radius: 3px;
  background-color: var(--bg-light);
  display: flex; align-items: center; justify-content: center;
  font-size: 7px; color: var(--text-primary); overflow: hidden;
}
@media (prefers-color-scheme: dark) { .cell { background-color: var(--bg-dark); } }
:root[data-theme="dark"] .cell { background-color: var(--bg-dark); }
:root[data-theme="light"] .cell { background-color: var(--bg-light); }
.cell-empty { background: var(--gridline) !important; opacity: 0.5; }
.cell[data-tip]:hover::after {
  content: attr(data-tip); position: absolute; bottom: 120%; left: 50%; transform: translateX(-50%);
  background: var(--text-primary); color: var(--surface-2); padding: 4px 8px; border-radius: 6px;
  font-size: 11px; white-space: nowrap; z-index: 10; pointer-events: none;
}
.hm-legend { display: flex; align-items: center; gap: 8px; margin-top: 10px; font-size: 11px; color: var(--text-secondary); }
.hm-legend-bar { flex: 1; max-width: 160px; height: 8px; border-radius: 4px;
  background: linear-gradient(to right, #cde2fb, #0d366b); }
:root[data-theme="dark"] .hm-legend-bar, @media (prefers-color-scheme: dark) { .hm-legend-bar { background: linear-gradient(to right, #23282e, #3987e5); } }

.slots-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.slots-table th { text-align: left; color: var(--text-secondary); font-weight: 500; padding: 6px 8px; border-bottom: 1px solid var(--gridline); }
.slots-table td { padding: 6px 8px; border-bottom: 1px solid var(--gridline); font-variant-numeric: tabular-nums; }
.slots-table td.num, .slots-table th.num { text-align: right; }

.diag-list { margin: 0; padding-left: 18px; font-size: 13px; line-height: 1.9; color: var(--text-secondary); }
details.raw { margin-top: 16px; }
details.raw summary { cursor: pointer; font-size: 13px; color: var(--text-secondary); }
details.raw pre { white-space: pre-wrap; font-size: 12px; background: var(--surface-2); border-radius: 8px; padding: 12px; margin-top: 8px; }
.footer { margin-top: 32px; font-size: 12px; color: var(--text-muted); }

.header-row { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
.refresh-area { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; min-width: 200px; }
.btn-refresh {
  font: inherit; font-size: 13px; font-weight: 600; padding: 8px 14px; border-radius: 8px;
  border: 1px solid var(--border); background: var(--surface-1); color: var(--text-primary); cursor: pointer;
}
.btn-refresh:hover:not(:disabled) { background: var(--gridline); }
.btn-refresh:disabled { opacity: 0.5; cursor: default; }
.confirm-panel {
  background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; font-size: 12px; color: var(--text-secondary); max-width: 240px; text-align: right;
}
.confirm-panel p { margin: 0 0 8px; }
.confirm-panel button { font: inherit; font-size: 12px; padding: 5px 10px; border-radius: 6px; cursor: pointer; margin-left: 6px; }
.btn-primary { background: var(--text-primary); color: var(--surface-2); border: 1px solid var(--text-primary); }
.btn-secondary { background: transparent; color: var(--text-secondary); border: 1px solid var(--border); }
.refresh-status { font-size: 12px; max-width: 260px; text-align: right; }
.refresh-status.running { color: var(--text-secondary); }
.refresh-status.ok { color: var(--good, #0ca30c); }
.refresh-status.error { color: var(--warning-ink); }

.pd-tabs { display: flex; gap: 8px; margin-top: 20px; border-bottom: 1px solid var(--gridline); }
.pd-tab-btn {
  font: inherit; font-size: 13px; font-weight: 600; padding: 8px 14px; cursor: pointer;
  background: none; border: none; border-bottom: 2px solid transparent; color: var(--text-secondary);
}
.pd-tab-btn.active { color: var(--text-primary); border-bottom-color: var(--text-primary); }
.pd-tab-panel[hidden] { display: none; }

.rs-card { display: flex; flex-direction: column; gap: 8px; }
.rs-card-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 13px; }
.rs-rank { font-weight: 700; color: var(--text-secondary); }
.rs-status-badge { margin-left: auto; font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); }
.rs-status-pending { color: var(--text-secondary); }
.rs-status-sent { color: var(--good, #0ca30c); border-color: var(--good, #0ca30c); }
.rs-status-skipped { color: var(--text-muted); }
.rs-nodata { color: var(--text-muted); }
.rs-original-text { font-size: 13px; color: var(--text-primary); white-space: pre-wrap; margin: 0; }
.rs-meta { font-size: 12px; color: var(--text-secondary); }
.rs-draft { background: var(--surface-2); border-radius: 8px; padding: 8px 10px; }
.rs-draft-label { font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
.rs-draft-text { font-size: 13px; white-space: pre-wrap; }
.rs-actions { display: flex; gap: 8px; margin-top: 4px; }
.rs-stale-warning { margin: 0; padding: 8px 12px; font-size: 12px; }

.rs-alert-banner {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  margin: 16px 0; padding: 12px 16px; border-radius: 8px; font-size: 14px; font-weight: 600;
  background: color-mix(in srgb, var(--warning) 22%, var(--surface-1));
  color: var(--warning-ink); border: 1px solid var(--warning);
}
.rs-response-list { display: flex; flex-direction: column; gap: 10px; margin: 12px 0; }
.rs-response-card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; }
.rs-response-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 12px; color: var(--text-secondary); }
.rs-response-text { font-size: 13px; white-space: pre-wrap; margin: 6px 0; }
.rs-response-links { display: flex; gap: 10px; font-size: 12px; }
.rs-response-overdue { color: var(--warning-ink); font-weight: 600; }
.rs-ack-btn { margin-top: 6px; }
</style>
"""

REFRESH_SCRIPT = """
<script>
(function () {
  function wireRefreshButton(ids, endpoint, runningText) {
    var btn = document.getElementById(ids.btn);
    var panel = document.getElementById(ids.panel);
    var yes = document.getElementById(ids.yes);
    var no = document.getElementById(ids.no);
    var status = document.getElementById(ids.status);
    if (!btn) return;

    btn.addEventListener('click', function () {
      panel.hidden = false;
      btn.disabled = true;
    });
    no.addEventListener('click', function () {
      panel.hidden = true;
      btn.disabled = false;
    });
    yes.addEventListener('click', function () {
      panel.hidden = true;
      status.textContent = runningText;
      status.className = 'refresh-status running';
      fetch(endpoint, { method: 'POST' })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) {
            status.textContent = (data.log || []).join(' / ') + ' 再読み込みします…';
            status.className = 'refresh-status ok';
            setTimeout(function () { location.reload(); }, 800);
          } else {
            status.textContent = 'エラー: ' + data.error;
            status.className = 'refresh-status error';
            btn.disabled = false;
          }
        })
        .catch(function () {
          status.textContent = 'ローカルサーバーに接続できません。"python main.py serve --account ACCOUNT_NAME" で起動してから開いてください。';
          status.className = 'refresh-status error';
          btn.disabled = false;
        });
    });
  }

  wireRefreshButton(
    { btn: 'pd-refresh-btn', panel: 'pd-confirm-panel', yes: 'pd-confirm-yes', no: 'pd-confirm-no', status: 'pd-refresh-status' },
    '/api/refresh', '取得中…'
  );
  wireRefreshButton(
    { btn: 'rs-refresh-btn', panel: 'rs-confirm-panel', yes: 'rs-confirm-yes', no: 'rs-confirm-no', status: 'rs-refresh-status' },
    '/api/reply_scout/refresh', '候補を収集し下書きを生成中…'
  );
  wireRefreshButton(
    { btn: 'rs-track-btn', panel: 'rs-track-confirm-panel', yes: 'rs-track-confirm-yes', no: 'rs-track-confirm-no', status: 'rs-track-status' },
    '/api/reply_scout/track', '反応を取得中…'
  );
})();
</script>
"""

TABS_SCRIPT = """
<script>
(function () {
  var STORAGE_KEY = 'pd-active-tab';
  var buttons = document.querySelectorAll('.pd-tab-btn');
  function activate(tab) {
    buttons.forEach(function (b) { b.classList.toggle('active', b.dataset.tab === tab); });
    document.querySelectorAll('.pd-tab-panel').forEach(function (panel) {
      panel.hidden = panel.id !== 'tab-' + tab;
    });
  }
  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () {
      try { localStorage.setItem(STORAGE_KEY, btn.dataset.tab); } catch (e) {}
      activate(btn.dataset.tab);
    });
  });
  var saved = null;
  try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) {}
  if (saved && document.getElementById('tab-' + saved)) {
    activate(saved);
  }
})();
</script>
"""

REPLY_SCOUT_SCRIPT = """
<script>
(function () {
  function extractId(input) {
    var trimmed = (input || '').trim();
    if (!trimmed) return null;
    var m = trimmed.match(/(\\d+)\\s*$/);
    return m ? m[1] : trimmed;
  }
  function updateStatus(candidateId, status, replyId) {
    return fetch('/api/reply_scout/status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ candidate_id: candidateId, status: status, reply_id: replyId || null }),
    }).then(function (r) { return r.json(); });
  }
  document.querySelectorAll('.rs-sent-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      if (btn.dataset.stale === '1') {
        var proceedStale = window.confirm(
          '⚠ この下書きは予測データ更新前に生成されたものです（枠順確定などで評価が' +
          '入れ替わっている可能性があります）。内容を確認せずに送信済みにしてよいですか？'
        );
        if (!proceedStale) return;
      }
      var id = btn.dataset.id;
      var replyUrl = window.prompt('送信したリプライのURLまたはID（反応を追跡しない場合は空欄でOK）:', '');
      if (replyUrl === null) return;
      updateStatus(id, '送信済み', extractId(replyUrl))
        .then(function (data) {
          if (data.ok) { location.reload(); }
          else { window.alert('エラー: ' + data.error); }
        })
        .catch(function () {
          window.alert('ローカルサーバーに接続できません。"python main.py serve" で起動してから開いてください。');
        });
    });
  });
  document.querySelectorAll('.rs-skip-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      updateStatus(btn.dataset.id, '見送り', null)
        .then(function (data) {
          if (data.ok) { location.reload(); }
          else { window.alert('エラー: ' + data.error); }
        })
        .catch(function () {
          window.alert('ローカルサーバーに接続できません。"python main.py serve" で起動してから開いてください。');
        });
    });
  });
  document.querySelectorAll('.rs-ack-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      fetch('/api/reply_scout/ack', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ response_id: btn.dataset.responseId }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.ok) { location.reload(); }
          else { window.alert('エラー: ' + data.error); }
        })
        .catch(function () {
          window.alert('ローカルサーバーに接続できません。"python main.py serve" で起動してから開いてください。');
        });
    });
  });
})();
</script>
"""


STATUS_CLASSES = {
    "未送信": "rs-status-pending",
    "送信済み": "rs-status-sent",
    "見送り": "rs-status-skipped",
}


def _reply_scout_candidate_card(
    account: Account, ranked: rs_db.RankedCandidate, current_predictions_hash: str | None
) -> str:
    c = ranked.candidate
    url = f"https://x.com/i/web/status/{c.id}"
    status_class = STATUS_CLASSES.get(ranked.status, "rs-status-pending")

    # 下書き生成後にRakuba予測データ（predictions.json等）が更新されているかの判定。
    # 枠順確定等で評価が入れ替わると、下書きが引用した事実文が公式投稿と矛盾しうる
    # （実例: ダノンセンチュリー◎→アンパドゥ◎の入れ替わりで前日の下書きが陳腐化）。
    # どちらかのハッシュが計算不能(None)な場合は比較不能として警告は出さない。
    stale = bool(
        ranked.drafts
        and ranked.predictions_hash
        and current_predictions_hash
        and ranked.predictions_hash != current_predictions_hash
    )
    stale_html = (
        '<p class="banner rs-stale-warning">⚠ 予測更新あり・再生成推奨'
        '（下書き生成後にRakuba予測データが更新されました。事実文が古い可能性があります）</p>'
        if stale
        else ""
    )

    if ranked.drafts:
        drafts_html = "".join(
            f'<div class="rs-draft"><div class="rs-draft-label">案{i}</div>'
            f'<div class="rs-draft-text">{html.escape(d)}</div></div>'
            for i, d in enumerate(ranked.drafts, start=1)
        )
    elif ranked.prediction_status == "予測データ未投入":
        drafts_html = '<p class="muted rs-nodata">予測データ未投入（対応するRakuba予測が見つかりませんでした）</p>'
    elif ranked.prediction_status == "時制スキップ":
        drafts_html = (
            '<p class="muted rs-nodata">時制スキップ（レース確定後の'
            'レース前投稿への返信になるため見送りました）</p>'
        )
    else:
        drafts_html = (
            '<p class="muted">下書き未生成です。'
            f'<code>python main.py reply-scout draft --account {html.escape(account.name)}</code> を実行してください。</p>'
        )

    followers = c.author_followers if c.author_followers is not None else "-"
    disabled = "" if ranked.status == "未送信" else "disabled"
    stale_attr = ' data-stale="1"' if stale else ""

    return f"""
    <div class="card rs-card" data-candidate-id="{html.escape(c.id, quote=True)}">
      <div class="rs-card-head">
        <span class="rs-rank">#{ranked.rank}</span>
        <a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">@{html.escape(c.author_screen_name)} の投稿を開く</a>
        <span class="rs-status-badge {status_class}">{html.escape(ranked.status)}</span>
      </div>
      <p class="rs-original-text">{html.escape(c.text)}</p>
      <div class="rs-meta">スコア {ranked.score:.2f} ／ いいね {c.likes} ／ フォロワー {followers} ／ キーワード「{html.escape(c.keyword)}」</div>
      {stale_html}
      {drafts_html}
      <div class="rs-actions">
        <button class="btn-refresh rs-sent-btn" data-id="{html.escape(c.id, quote=True)}" {disabled}{stale_attr}>送信済みにする</button>
        <button class="btn-refresh rs-skip-btn" data-id="{html.escape(c.id, quote=True)}" {disabled}>見送り</button>
      </div>
    </div>
    """


def _reply_scout_section(account: Account) -> str:
    with rs_db.connect(account) as conn:
        ranked_list = rs_db.get_top_candidates(conn)

    if not ranked_list:
        return (
            '<div class="card"><p class="muted">まだ候補がありません。'
            f'<code>python main.py reply-scout run --account {html.escape(account.name)}</code> を実行してください。</p></div>'
        )

    scout_cfg = rs_load_config()
    pcfg = rs_prediction_data.load_prediction_config(scout_cfg.rakuba_output_dir)
    current_predictions_hash = rs_prediction_data.compute_predictions_signature(pcfg)

    cards = "".join(
        _reply_scout_candidate_card(account, r, current_predictions_hash) for r in ranked_list
    )
    return f'<div class="card-grid">{cards}</div>'


JST = timezone(timedelta(hours=9))


def _format_utc_as_jst(value: str | None) -> str | None:
    """sent_at/last_checked_at(reply_scout/db.pyの_utc_now_iso()で保存、UTC)をJST表示に変換する。

    過去分はオフセット無しの'YYYY-MM-DD HH:MM:SS'(暗黙にUTC)で保存されている
    ことがある(_utc_now_iso()導入前のレコード)ため、tzinfoが無ければUTCとみなす。
    以前はこの変換をせずUTC文字列をそのまま表示しており、実際の送信時刻から
    9時間ズレて見えるバグがあった。
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(JST).strftime("%Y-%m-%d %H:%M")


OVERDUE_HOURS = 48


def _elapsed_hours(created_at_jst_iso: str) -> float:
    dt = datetime.fromisoformat(created_at_jst_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)
    return (datetime.now(JST) - dt.astimezone(JST)).total_seconds() / 3600


def _format_elapsed(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)}分前"
    if hours < 24:
        return f"{int(hours)}時間前"
    return f"{int(hours // 24)}日前"


def _unacknowledged_responses_section(account: Account) -> str:
    with rs_db.connect(account) as conn:
        details = rs_db.get_unacknowledged_responses(conn)

    if not details:
        return ""

    cards = []
    for d in details:
        r = d.response
        hours = _elapsed_hours(r.created_at)
        overdue_html = (
            f'<span class="rs-response-overdue">⚠早めの返信推奨（{int(hours)}時間経過）</span>'
            if hours > OVERDUE_HOURS
            else ""
        )
        response_url = f"https://x.com/i/web/status/{r.id}"
        parent_url = f"https://x.com/i/web/status/{r.parent_reply_id}"
        author = f"@{r.author_username}" if r.author_username else r.author_id
        cards.append(f"""
        <div class="rs-response-card">
          <div class="rs-response-head">
            <strong>{html.escape(author)}</strong>
            <span>{_format_elapsed(hours)}</span>
            {overdue_html}
          </div>
          <p class="rs-response-text">{html.escape(r.text)}</p>
          <div class="rs-response-links">
            <a href="{html.escape(response_url, quote=True)}" target="_blank" rel="noopener">この返信を開く</a>
            <a href="{html.escape(parent_url, quote=True)}" target="_blank" rel="noopener">元の自分のリプを開く</a>
          </div>
          <button class="btn-refresh rs-ack-btn" data-response-id="{html.escape(r.id, quote=True)}">対応済みにする</button>
        </div>
        """)

    return f"""
    <div class="rs-alert-banner">⚠ 未対応の返信 {len(details)}件</div>
    <div class="rs-response-list">{"".join(cards)}</div>
    """


def _sent_replies_section(account: Account) -> str:
    with rs_db.connect(account) as conn:
        sent = rs_db.get_sent_reply_details(conn)

    if not sent:
        return '<div class="card"><p class="muted">送信済みリプライはまだありません。</p></div>'

    rows = []
    for s in sent:
        reply_url = f"https://x.com/i/web/status/{s.reply_id}"
        target = f"@{s.author_screen_name}" if s.author_screen_name else "-"
        checked = _format_utc_as_jst(s.last_checked_at) or "未追跡"
        rows.append(
            "<tr>"
            f"<td>{_format_utc_as_jst(s.sent_at) or '-'}</td>"
            f"<td><a href='{html.escape(reply_url, quote=True)}' target='_blank' rel='noopener'>リプライを開く</a></td>"
            f"<td>{html.escape(target)}</td>"
            f"<td class='num'>{s.impressions:,}</td>"
            f"<td class='num'>{s.likes:,}</td>"
            f"<td class='num'>{s.retweets:,}</td>"
            f"<td class='num'>{s.other_reply_count:,}</td>"
            f"<td>{checked}</td>"
            "</tr>"
        )
    return f"""
    <div class="card">
      <table class="slots-table">
        <thead>
          <tr>
            <th>送信日時(JST)</th><th>リプライ</th><th>対象アカウント</th>
            <th class="num">インプレッション</th><th class="num">いいね</th>
            <th class="num">RT</th><th class="num">他者からの返信数</th><th>最終追跡(JST)</th>
          </tr>
        </thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
      <p class="muted" style="margin-top:8px;">📈 反応を追跡 ボタンで最新の値に更新できます。他者からの返信数は直近14日以内に送信したリプライのみ検知対象です。</p>
    </div>
    """


def render_fragment(df: pd.DataFrame, account: Account, prescription_text: str) -> str:
    diag = diagnose(df)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    last_post = df["created_at"].max() if len(df) else None

    banner = ""
    if diag.low_confidence:
        banner = (
            '<div class="banner">⚠️ サンプル数が少なく'
            f'（{diag.n}件 &lt; {MIN_POSTS_FOR_CONFIDENCE}件）、以下は参考値です。'
            "データが増え次第、傾向が変わる可能性があります。</div>"
        )

    stats = "".join(
        [
            _stat_tile("分析対象投稿数", f"{diag.n}件"),
            _stat_tile("平均インプレッション", f"{diag.avg_impressions:,.0f}"),
            _stat_tile("平均エンゲージメント率", f"{diag.avg_engagement_rate:.2f}%"),
            _stat_tile("最新投稿", last_post[:16] if last_post else "-"),
        ]
    )

    heatmap_cards = "".join(_heatmap_card(df, m) for m in METRICS)

    slot_cards = _slots_table(df, "impressions", "おすすめ投稿枠 TOP5（インプレッション重視）") + _slots_table(
        df, "engagement_rate", "おすすめ投稿枠 TOP5（エンゲージメント率重視）"
    )

    diag_items = [
        f"分析対象投稿数: {diag.n}件",
        f"平均インプレッション: {diag.avg_impressions:,.0f}",
        f"平均エンゲージメント率: {diag.avg_engagement_rate:.2f}%",
    ]
    if diag.weekday_avg is not None and diag.weekend_avg is not None:
        if diag.weekend_avg > diag.weekday_avg * 1.1:
            diag_items.append(
                f"週末の方が平日よりエンゲージメント率が高い傾向（週末 {diag.weekend_avg:.2f}% vs 平日 {diag.weekday_avg:.2f}%）"
            )
        elif diag.weekday_avg > diag.weekend_avg * 1.1:
            diag_items.append(
                f"平日の方が週末よりエンゲージメント率が高い傾向（平日 {diag.weekday_avg:.2f}% vs 週末 {diag.weekend_avg:.2f}%）"
            )
    diag_html = "".join(f"<li>{d}</li>" for d in diag_items)

    refresh_script = REFRESH_SCRIPT.replace("ACCOUNT_NAME", account.name)
    unacknowledged_responses_section = _unacknowledged_responses_section(account)
    reply_scout_section = _reply_scout_section(account)
    sent_replies_section = _sent_replies_section(account)

    return f"""
{STYLE}
<div class="pd-dash">
  <div class="header-row">
    <div>
      <h1>PostDoctor ダッシュボード</h1>
      <div class="meta">@{account.screen_name} ・ 生成日時 {now}</div>
    </div>
  </div>

  <div class="pd-tabs">
    <button class="pd-tab-btn active" data-tab="analysis">投稿分析</button>
    <button class="pd-tab-btn" data-tab="reply-scout">リプライ営業</button>
  </div>

  <div id="tab-analysis" class="pd-tab-panel">
  <div class="header-row">
    <div></div>
    <div class="refresh-area">
      <button id="pd-refresh-btn" class="btn-refresh">🔄 更新</button>
      <div id="pd-confirm-panel" class="confirm-panel" hidden>
        <p>最新データを取得して再生成します。よろしいですか？</p>
        <button id="pd-confirm-no" class="btn-secondary">キャンセル</button>
        <button id="pd-confirm-yes" class="btn-primary">実行</button>
      </div>
      <div id="pd-refresh-status" class="refresh-status"></div>
    </div>
  </div>
  {banner}

  <div class="stat-row">{stats}</div>

  <h2>曜日×時間帯ヒートマップ</h2>
  <div class="card-grid">{heatmap_cards}</div>

  <h2>おすすめ投稿枠</h2>
  <div class="card-grid">{slot_cards}</div>

  <h2>処方箋（診断）</h2>
  <div class="card">
    <ul class="diag-list">{diag_html}</ul>
    <details class="raw">
      <summary>処方箋 Markdown 全文を表示</summary>
      <pre>{prescription_text}</pre>
    </details>
  </div>
  </div>

  <div id="tab-reply-scout" class="pd-tab-panel" hidden>
  <div class="header-row">
    <h2 style="margin:0;">リプライ営業 候補 TOP10</h2>
    <div class="refresh-area">
      <div style="display:flex; gap:8px;">
        <button id="rs-refresh-btn" class="btn-refresh">🔍 候補を更新</button>
        <button id="rs-track-btn" class="btn-refresh">📈 反応を追跡</button>
      </div>
      <div id="rs-confirm-panel" class="confirm-panel" hidden>
        <p>新しい候補を検索し下書きを生成します。APIコストが発生します（検索$0.005/件、フォロワー取得$0.010/件、日次上限あり）。よろしいですか？</p>
        <button id="rs-confirm-no" class="btn-secondary">キャンセル</button>
        <button id="rs-confirm-yes" class="btn-primary">実行</button>
      </div>
      <div id="rs-track-confirm-panel" class="confirm-panel" hidden>
        <p>送信済みリプライの反応を取得します（自分の投稿の読み取りのみ、$0.001/件と低コストです）。よろしいですか？</p>
        <button id="rs-track-confirm-no" class="btn-secondary">キャンセル</button>
        <button id="rs-track-confirm-yes" class="btn-primary">実行</button>
      </div>
      <div id="rs-refresh-status" class="refresh-status"></div>
      <div id="rs-track-status" class="refresh-status"></div>
    </div>
  </div>
  {unacknowledged_responses_section}
  {reply_scout_section}

  <h2>送信済みリプライの反応</h2>
  {sent_replies_section}
  <div class="footer">
    候補収集・下書き生成・反応追跡・送信済み/見送りの記録は、いずれもローカルサーバー経由
    （python main.py serve --account {account.name}）で開いた時のみ動作します。
    送信そのものは自動化していません。必ずXアプリ等から手動で送信してください。
  </div>
  </div>

  <div class="footer">
    「更新」ボタンはローカルサーバー経由（python main.py serve --account {account.name}）で開いた時のみ動作します。
    それ以外の場合は python main.py run --account {account.name} を手動で実行してください。
  </div>
</div>
{refresh_script}
{TABS_SCRIPT}
{REPLY_SCOUT_SCRIPT}
"""


def generate_dashboard(df: pd.DataFrame, account: Account, prescription_text: str) -> str:
    fragment = render_fragment(df, account, prescription_text)
    page = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PostDoctor - @{account.screen_name}</title>
</head>
<body>
{fragment}
</body>
</html>
"""
    out_path = account.data_dir / "dashboard.html"
    out_path.write_text(page, encoding="utf-8")
    return str(out_path)
