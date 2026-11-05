"""Generate docs/demo.gif: an animated walkthrough of the PNR alert (terminal on the left, phone pushes
on the right). Dev-only helper; needs Pillow (`pip install pillow`). Run: python docs/make_demo_gif.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 960, 500
BG, PANEL, BORDER = (13, 17, 23), (22, 27, 34), (48, 54, 61)
TEXT, DIM = (230, 237, 243), (139, 148, 158)
GREEN, BLUE, AMBER, RED = (63, 185, 80), (88, 166, 255), (210, 153, 34), (248, 81, 73)

FONTS = r"C:\Windows\Fonts"


def font(name: str, size: int):
    try:
        return ImageFont.truetype(rf"{FONTS}\{name}", size)
    except OSError:
        return ImageFont.load_default()


MONO, MONO_B = font("consola.ttf", 15), font("consolab.ttf", 15)
UI, UI_B, UI_S = font("segoeui.ttf", 15), font("segoeuib.ttf", 16), font("segoeui.ttf", 13)
TITLE, CAP = font("segoeuib.ttf", 26), font("segoeuib.ttf", 19)

# (time, terminal text, terminal colour, push title, push body, push colour, caption)
STEPS = [
    ("Nov 5  17:30", "check -> WL 35", TEXT, "Tracking started", "WL 35", BLUE,
     "1. First check: it records your waitlist position"),
    ("Nov 5  18:30", "check -> WL 35   (no change, no alert)", DIM, None, None, None,
     "2. No change means silence: no spam on your phone"),
    ("Nov 6  09:30", "check -> WL 28", GREEN, "Waitlist dropped!", "WL 35 -> WL 28", GREEN,
     "3. The waitlist drops: instant push"),
    ("Nov 7  18:40", "check -> WL 12   (chart window: every 10 min)", GREEN, "Waitlist dropped!",
     "WL 28 -> WL 12", GREEN, "4. Near departure it checks every 10 minutes"),
    ("Nov 7  19:10", "check -> RAC 4", AMBER, "Moved to RAC!", "WL 12 -> RAC 4", AMBER,
     "5. Moved to RAC"),
    ("Nov 7  19:40", "check -> CNF/B3/42", GREEN, "PNR CONFIRMED!", "RAC 4 -> CNF/B3/42", GREEN,
     "6. Confirmed: polling stops by itself"),
]
FINAL_LINE = "ticket confirmed -> workflow disabled, polling stopped"


def rounded(d, box, fill, outline=None, r=10):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=1)


def draw_frame(step_idx: int, show_push: bool, intro: bool = False, outro: bool = False) -> Image.Image:
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)

    if intro:
        d.text((W // 2, 190), "PNR waitlist alert", font=TITLE, fill=TEXT, anchor="mm")
        d.text((W // 2, 235), "A push on your phone when your waitlist moves", font=UI_B, fill=DIM, anchor="mm")
        d.text((W // 2, 285), "Python  -  RapidAPI  -  ntfy  -  GitHub Actions (free)", font=UI, fill=BLUE, anchor="mm")
        return im

    # caption
    caption = STEPS[step_idx][6]
    d.text((24, 22), caption, font=CAP, fill=TEXT)

    # terminal panel
    rounded(d, (24, 70, 600, 460), PANEL, BORDER)
    d.rectangle((25, 71, 599, 98), fill=(33, 38, 45))
    for i, c in enumerate((RED, AMBER, GREEN)):
        d.ellipse((38 + i * 20, 78, 50 + i * 20, 90), fill=c)
    d.text((120, 77), "pnr_alert.py  (GitHub Actions, wakes every 10 min)", font=UI_S, fill=DIM)

    lines = []
    for i in range(step_idx + 1):
        t, txt, col, *_ = STEPS[i]
        lines.append((f"{t}  ", DIM, txt, col))
    if outro:
        lines.append(("", DIM, FINAL_LINE, BLUE))
    y = 112
    for stamp, scol, txt, col in lines[-12:]:
        d.text((40, y), stamp, font=MONO, fill=scol)
        d.text((40 + d.textlength(stamp, font=MONO), y), txt, font=MONO_B if col != DIM else MONO, fill=col)
        y += 28

    # phone
    rounded(d, (650, 40, 930, 470), (10, 13, 18), (88, 96, 105), r=34)
    rounded(d, (668, 78, 912, 440), (17, 21, 27), None, r=18)
    d.text((790, 56), "ntfy", font=UI_S, fill=DIM, anchor="mm")
    d.text((790, 100), "Your phone", font=UI_S, fill=DIM, anchor="mm")

    pushes = []
    for i in range(step_idx + (1 if show_push else 0)):
        _, _, _, title, body, color, _ = STEPS[i]
        if title:
            pushes.append((title, body, color))
    y = 122
    for title, body, color in reversed(pushes[-3:]):  # newest first
        rounded(d, (678, y, 902, y + 82), (33, 38, 45), BORDER, r=12)
        d.rectangle((678, y + 14, 682, y + 68), fill=color)
        d.text((694, y + 12), title, font=UI_B, fill=TEXT)
        d.text((694, y + 40), body, font=MONO, fill=color)
        d.text((890, y + 12), "now" if title == pushes[-1][0] and body == pushes[-1][1] else "", font=UI_S,
               fill=DIM, anchor="ra")
        y += 96
    if not pushes:
        d.text((790, 250), "no notifications yet", font=UI_S, fill=DIM, anchor="mm")

    # progress dots
    for i in range(len(STEPS)):
        c = BLUE if i == step_idx else BORDER
        d.ellipse((24 + i * 22, 478, 34 + i * 22, 488), fill=c)
    return im


def main() -> None:
    frames, durations = [], []

    def add(im, ms):
        frames.append(im)
        durations.append(ms)

    add(draw_frame(0, False, intro=True), 1800)
    for i, step in enumerate(STEPS):
        add(draw_frame(i, False), 900)             # terminal shows the new check
        add(draw_frame(i, True), 1900 if step[3] else 1500)  # then the push arrives (or not)
    add(draw_frame(len(STEPS) - 1, True, outro=True), 3500)

    out = Path(__file__).with_name("demo.gif")
    pal = [f.convert("P", palette=Image.ADAPTIVE, colors=64) for f in frames]
    pal[0].save(out, save_all=True, append_images=pal[1:], duration=durations, loop=0, optimize=True)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB, {len(frames)} frames)")


if __name__ == "__main__":
    main()
