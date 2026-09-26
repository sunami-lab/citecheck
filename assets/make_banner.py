"""Draw assets/banner.png from a real citecheck JSON report of the example paper.

    python3 scripts/citecheck.py examples/refs.bib --cited-in examples/paper.tex --json /tmp/example.json
    python3 assets/make_banner.py /tmp/example.json

Needs Pillow and the DejaVu fonts (standard on Linux).
"""
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

FONTS = "/usr/share/fonts/truetype/dejavu/"
W, H, PAD = 1800, 880, 90
BG, CARD, EDGE = (11, 11, 14), (21, 21, 26), (42, 42, 49)
TEXT, DIM = (236, 236, 241), (139, 139, 150)
# Colour-blind-safe hues; the symbol repeats the verdict so colour is never the only cue.
STYLE = {"VERIFIED": ("✓", (62, 207, 142)), "CHECK": ("!", (245, 182, 66)),
         "MISMATCH": ("✗", (255, 107, 91)), "NOT_FOUND": ("✗", (255, 107, 91)),
         "ERROR": ("?", (139, 139, 150)), "SKIPPED": ("-", (139, 139, 150))}
REASONS = [("cited authors are not on", "wrong authors"), ("no work with this title", "no such paper"),
           ("does not exist", "ID does not exist"), ("different work", "ID points elsewhere"),
           ("title differs", "title differs"), ("URL is live", "web page")]


def font(name, size):
    return ImageFont.truetype(os.path.join(FONTS, name), size)


def fit(draw, text, fnt, width):
    if draw.textlength(text, font=fnt) <= width:
        return text
    while draw.textlength(text + "…", font=fnt) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def who(r):
    fams = [a.split(",")[0].split()[-1] if "," in a else a.split()[-1] for a in r["cited"]["authors"]]
    names = fams[0] + " et al." if len(fams) > 2 else " & ".join(fams)
    return f"{names} {r['cited']['year'] or ''}".strip()


def reason(r):
    for issue in r["issues"]:
        if "cited year" in issue:
            return f"year is {r['match']['year']}"
        for needle, label in REASONS:
            if needle in issue:
                return label
    return ""


def main(report_path):
    with open(report_path, encoding="utf-8") as fh:
        results = json.load(fh)["results"]
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    bold, sans, mono = font("DejaVuSans-Bold.ttf", 104), font("DejaVuSans.ttf", 28), font("DejaVuSansMono.ttf", 28)

    d.text((PAD, 70), "cite", font=bold, fill=TEXT)
    d.text((PAD + d.textlength("cite", font=bold), 70), "check", font=bold, fill=STYLE["VERIFIED"][1])
    d.text((PAD + 4, 205), "Catch hallucinated references before your reviewers do.", font=font("DejaVuSans.ttf", 40), fill=DIM)

    top, row = 290, 62
    bottom = top + 40 + 50 + 20 + row * len(results) + 20
    d.rounded_rectangle((PAD, top, W - PAD, bottom), radius=24, fill=CARD, outline=EDGE, width=2)
    x0 = PAD + 40
    d.text((x0, top + 36), "$", font=mono, fill=STYLE["VERIFIED"][1])
    d.text((x0 + 34, top + 36), "python3 citecheck.py refs.bib --cited-in paper.tex", font=mono, fill=DIM)

    pill_font, reason_font = font("DejaVuSansMono-Bold.ttf", 24), font("DejaVuSans.ttf", 24)
    for i, r in enumerate(results):
        y = top + 40 + 50 + 20 + i * row
        symbol, colour = STYLE[r["verdict"]]
        d.text((x0, y), f"[{i + 1}]", font=mono, fill=DIM)
        d.text((x0 + 70, y), who(r), font=mono, fill=(200, 200, 208))
        d.text((x0 + 430, y), fit(d, r["cited"]["title"], sans, 660), font=sans, fill=TEXT)
        px = x0 + 1110
        tint = tuple(int(c * 0.2 + b * 0.8) for c, b in zip(colour, CARD))
        d.rounded_rectangle((px, y - 6, px + 200, y + 38), radius=22, fill=tint)
        label = f"{symbol} {r['verdict']}"
        d.text((px + (200 - d.textlength(label, font=pill_font)) / 2, y), label, font=pill_font, fill=colour)
        d.text((px + 220, y + 2), fit(d, reason(r), reason_font, W - PAD - 40 - px - 220), font=reason_font, fill=DIM)

    d.text((W / 2, bottom + 38), "Semantic Scholar  ·  OpenAlex  ·  Crossref  ·  arXiv  ·  doi.org",
           font=font("DejaVuSans.ttf", 26), fill=DIM, anchor="mt")
    img = img.crop((0, 0, W, bottom + 100))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.png")
    img.save(out, optimize=True)
    print(out, img.size)


if __name__ == "__main__":
    main(sys.argv[1])
