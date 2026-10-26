"""Render docs/demo.svg: a terminal window replaying one real (offline) trade
through /nse-entry, /nse-exit and /nse-timemachine. The lines are copied from
the scripts' own output on the test fixtures (RELIANCE, 2026-10-26).

    py _shared/build/demo_svg.py
"""

import os
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (text, style): p prompt, c command, d dim, g green, r red, y yellow, b bold, "" plain
LINES = [
    ("> /nse-entry RELIANCE", "c"),
    ("TRIGGERED 10:00 AM at ₹1,193.60: the 9:55 bar closed above the range high ₹1,192.00", "g"),
    ("Closing auction: F&O stock, continuous trading ends 15:15. Last new entry 14:30.", "y"),
    ("", ""),
    ("> /nse-exit", "c"),
    ("  Entry         ₹1,193.60 at 10:00 AM", ""),
    ("  Stop          ₹1,183.65  opening-range midpoint", ""),
    ("  Target        ₹1,213.50  2R", ""),
    ("  Reward:risk   2.0 before charges, 1.75 after", "b"),
    ("  Charges       ₹89.27 at the stop (brokerage, STT, exchange, SEBI, stamp, GST)", "d"),
    ("  Flat by       15:07 IST   (Zerodha squares off F&O MIS at 15:12)", "y"),
    ("", ""),
    ("> /nse-timemachine", "c"),
    ("10,000 replays from RELIANCE's own last 58 sessions, out by 15:05", "d"),
    ("  target first   2.2 %", ""),
    ("  stop first    30.2 %", ""),
    ("  neither       67.6 %", ""),
    ("  Average R: -0.164 (net of charges)", "r"),
    ("Strongly toward the stop: after charges the 2R target needs to come first in", "r"),
    ("36 % of decided replays; it did in 7 %.", "r"),
    ("", ""),
    ("Skip this one. Nothing was placed.", "b"),
]

COLORS = {"c": "#7dd3fc", "d": "#8b949e", "g": "#4ade80", "r": "#f87171", "y": "#fbbf24", "b": "#f0f6fc", "": "#d1d7e0"}
W, LH, PAD, TOP = 860, 22, 22, 50


def render():
    h = TOP + PAD + LH * len(LINES) + PAD
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
           'aria-label="India Trader demo: a RELIANCE breakout planned, replayed 10,000 times and skipped">' % (W, h, W, h),
           '<rect width="%d" height="%d" rx="10" fill="#0d1117"/>' % (W, h),
           '<rect width="%d" height="34" rx="10" fill="#161b22"/><rect y="24" width="%d" height="10" fill="#161b22"/>' % (W, W)]
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        out.append('<circle cx="%d" cy="17" r="6" fill="%s"/>' % (20 + i * 20, c))
    out.append('<text x="%d" y="22" fill="#8b949e" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" '
               'font-size="13" text-anchor="middle">claude  —  india-trader</text>' % (W // 2))
    y = TOP + PAD
    for text, st in LINES:
        if text:
            weight = ' font-weight="700"' if st in ("b", "c") else ""
            out.append('<text x="%d" y="%d" fill="%s" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" '
                       'font-size="14" xml:space="preserve"%s>%s</text>' % (PAD, y, COLORS[st], weight, escape(text)))
        y += LH
    out.append("</svg>")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    path = os.path.join(ROOT, "docs", "demo.svg")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render())
    print("wrote", path)
