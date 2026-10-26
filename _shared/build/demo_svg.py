"""Render docs/demo.svg: a terminal window replaying one real live session,
Mon 2026-10-26 from 11:38 to 11:49 IST: /nse-scanner on the Nifty 500, then
/nse-news, /nse-entry, /nse-exit and /nse-timemachine on TCS and HCLTECH.
Every number is copied from the scripts' own output in that session (paper,
broker zerodha, nothing placed).

    py _shared/build/demo_svg.py
"""

import os
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (text, style): c command, d dim, g green, r red, y yellow, b bold, "" plain
LINES = [
    ("> Scan the Nifty 500 for breakouts", "c"),
    ("499 of 501 charts: 5 twenty-day breakouts, 12 bull flags, 172 opening-range breaks by 11:41", ""),
    ("On both lists: TCS  INFY  HCLTECH  COFORGE  PERSISTENT  MPHASIS  TATAELXSI  ...", "g"),
    ("", ""),
    ("> run for tcs and hcltech", "c"),
    ("news   TCS ₹2,520.80 -0.36%   HCLTECH ₹1,414.80 -0.52%   (21 loose headlines hidden)", "d"),
    ("       \"Tata Consultancy Services Stock Update: Shares Dip Amid IT Sector Concerns\"", "d"),
    ("entry  TCS TRIGGERED 9:55 AM at ₹2,533.20    HCLTECH TRIGGERED 10:10 AM at ₹1,415.20", "g"),
    ("       F&O stocks: continuous trading ends 15:15 (closing auction). Last new entry 14:30.", "y"),
    ("exit   TCS     stop ₹2,510.05  target ₹2,579.50", ""),
    ("       HCLTECH stop ₹1,401.80  target ₹1,442.00", ""),
    ("       Reward:risk 2.0 before charges, 1.76 after (₹85.57 of STT, brokerage, GST... at the stop)", "b"),
    ("       Flat by 15:07 IST: Zerodha squares off F&O MIS at 15:12", "y"),
    ("", ""),
    ("time machine, 10,000 replays each     target first   stop first   average R after charges", "d"),
    ("       TCS                                 4.3 %        47.3 %        -0.33", ""),
    ("       HCLTECH                             4.3 %        45.0 %        -0.30", ""),
    ("       Same rule on the last 58 sessions: TCS -0.22R, HCLTECH -0.31R", "d"),
    ("       Strongly toward the stop on both.", "r"),
    ("", ""),
    ("Skip both. Nothing was placed.", "b"),
]

COLORS = {"c": "#7dd3fc", "d": "#8b949e", "g": "#4ade80", "r": "#f87171", "y": "#fbbf24", "b": "#f0f6fc", "": "#d1d7e0"}
W, LH, PAD, TOP = 900, 22, 22, 50


def render():
    h = TOP + PAD + LH * len(LINES) + PAD
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d" role="img" '
           'aria-label="Dalal Street Skills demo: a live Nifty 500 scan, then TCS and HCLTECH breakouts planned, replayed 10,000 times and skipped">' % (W, h, W, h),
           '<rect width="%d" height="%d" rx="10" fill="#0d1117"/>' % (W, h),
           '<rect width="%d" height="34" rx="10" fill="#161b22"/><rect y="24" width="%d" height="10" fill="#161b22"/>' % (W, W)]
    for i, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        out.append('<circle cx="%d" cy="17" r="6" fill="%s"/>' % (20 + i * 20, c))
    out.append('<text x="%d" y="22" fill="#8b949e" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" '
               'font-size="13" text-anchor="middle">claude  —  dalal-street-skills</text>' % (W // 2))
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
