"""Self-contained HTML/JSON reports (no external assets, no network)."""

from __future__ import annotations

import html
import json

CSS = """body{background:#15171a;color:#d8dde3;font:14px Segoe UI,Arial,sans-serif;margin:24px}
h1,h2{color:#fff;font-weight:600}table{border-collapse:collapse;margin:8px 0 18px}td,th{border:1px solid #333;
padding:4px 8px;text-align:left;vertical-align:top}th{background:#22262b}.PASS,.TECHNICAL_PASS{color:#5fd38d}
.FAILED,.FAIL{color:#ff6b6b}.NEEDS_LISTENING_REVIEW,.WARN,.UNCERTAIN,.DRAFT_NOT_RELEASE_READY{color:#ffcc66}
.small{color:#8a939c;font-size:12px}svg{background:#1d2024;border:1px solid #333}code{color:#9cdcfe}"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _kv_table(d: dict) -> str:
    rows = []
    for k, v in d.items():
        if isinstance(v, (dict, list)):
            v = json.dumps(v, ensure_ascii=False)
            if len(v) > 400:
                v = v[:400] + " ..."
        rows.append(f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>")
    return "<table>" + "".join(rows) + "</table>"


def _polyline(values, w=900, h=140, lo=-40.0, hi=0.0, color="#4ea1ff") -> str:
    vals = [v for v in values]
    if not vals:
        return ""
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        if v is None:
            continue
        y = h - (max(lo, min(hi, v)) - lo) / (hi - lo) * h
        pts.append(f"{i * w / max(1, n - 1):.1f},{y:.1f}")
    return f'<polyline fill="none" stroke="{color}" stroke-width="1" points="{" ".join(pts)}"/>'


def _wave_svg(wave: dict, markers: list[dict], duration: float, w=900, h=120) -> str:
    peaks = wave.get("peak", [])
    n = len(peaks)
    if not n:
        return ""
    bars = []
    for i, p in enumerate(peaks):
        x = i * w / n
        y = (1 - min(1.0, p)) * h / 2
        bars.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{x:.1f}" y2="{h - y:.1f}" stroke="#4ea1ff" stroke-width="0.8"/>')
    for m in markers[:200]:
        x = m["start_s"] / max(duration, 1e-9) * w
        col = "#ff6b6b" if m.get("severity") in ("SEVERE", "MODERATE") else "#ffcc66"
        bars.append(f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{h}" stroke="{col}" stroke-width="1">'
                    f'<title>{esc(m["type"])} @ {m["start_s"]}s</title></line>')
    return f'<svg width="{w}" height="{h}">{"".join(bars)}</svg>'


def track_report_html(title: str, report: dict) -> str:
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(title)}</title><style>{CSS}</style>"
             f"</head><body><h1>{esc(title)}</h1>"]
    qc = report.get("qc", {})
    if qc:
        parts.append(f"<h2>Technical QA: <span class='{esc(qc.get('technical_status'))}'>"
                     f"{esc(qc.get('technical_status'))}</span></h2>")
        parts.append(f"<p class='small'>{esc(qc.get('note'))} Listening sign-off: <b>{esc(qc.get('listening_signoff'))}</b></p>")
        parts.append(_kv_table({"failures": qc.get("failures"), "warnings": qc.get("warnings")}))
    src = report.get("source", {})
    if src:
        parts.append("<h2>Source / provenance</h2>" + _kv_table(src))
    for label, key in (("Measurements (source)", "measurements"), ("Measurements (output)", "output_measurements")):
        m = report.get(key)
        if not m:
            continue
        parts.append(f"<h2>{label}</h2>")
        for sec in ("loudness", "peaks", "dynamics", "stereo", "spectral", "silence", "tempo"):
            if sec in m:
                parts.append(f"<h3>{sec}</h3>" + _kv_table(m[sec]))
        tl = m.get("timelines")
        if tl:
            parts.append("<h3>Short-term (blue) / momentary (grey) loudness, -40..0 LUFS</h3>")
            parts.append('<svg width="900" height="140">' + _polyline(tl.get("momentary_lufs", []), color="#666")
                         + _polyline(tl.get("short_term_lufs", [])) + "</svg>")
            parts.append("<h3>Waveform peaks with candidate markers</h3>")
            parts.append(_wave_svg(tl.get("waveform", {}), m.get("defects", {}).get("candidates", []),
                                   m.get("duration_s", 1)))
        st = m.get("style")
        if st:
            parts.append("<h3>Style hypotheses (heuristic, LOW confidence)</h3><table><tr><th>style</th><th>score</th>"
                         "<th>evidence</th></tr>" + "".join(
                             f"<tr><td>{esc(r['label'])}</td><td>{r['score']}</td><td>{esc('; '.join(r['evidence']))}</td></tr>"
                             for r in st.get("ranked", [])[:5]) + "</table>"
                         f"<p class='small'>{esc(st.get('disclaimer'))} Prompt-declared: "
                         f"{esc(st.get('prompt_declared_styles'))} -> {esc(st.get('prompt_vs_audio'))}</p>")
        d = m.get("defects")
        if d:
            parts.append("<h3>Defect candidates</h3><table><tr><th>type</th><th>time (s)</th><th>ch</th><th>severity</th>"
                         "<th>confidence</th><th>evidence</th><th>action</th></tr>" + "".join(
                             f"<tr><td>{esc(c['type'])}</td><td>{c['start_s']}-{c['end_s']}</td><td>{esc(c['channel'])}</td>"
                             f"<td class='{esc(c['severity'])}'>{esc(c['severity'])}</td><td>{esc(c['confidence'])}</td>"
                             f"<td>{esc(json.dumps(c['evidence'], ensure_ascii=False))}</td><td>{esc(c['recommended_action'])}</td></tr>"
                             for c in d.get("candidates", [])[:300]) + "</table>")
            parts.append("<p class='small'>" + esc(d.get("note")) + "</p><ul>" + "".join(
                f"<li>{esc(f['description'])}: <b>{esc(f['status'])}</b></li>" for f in d.get("listening_required", []))
                + "</ul>")
    for key in ("processing", "config", "comparison", "validation"):
        if report.get(key):
            parts.append(f"<h2>{esc(key)}</h2><pre>{esc(json.dumps(report[key], indent=2, ensure_ascii=False))}</pre>")
    parts.append("</body></html>")
    return "".join(parts)


def strip_timelines(m: dict | None) -> dict | None:
    if not m:
        return m
    return {k: v for k, v in m.items() if k != "timelines"}
