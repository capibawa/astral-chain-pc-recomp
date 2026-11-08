"""Button glyphs for the Xbox and PlayStation prompts (tools/make_prompts_folders.py), drawn from shapes and strokes.

The game's button icons (ui_core's core.wtp, texture 0x79c096fa) are white bodies about 22 pixels across with the
symbol cut out as transparency: a circle for A, B, X, Y, Plus and Minus, a box with a rounded corner for L, R, ZL and
ZR. These glyphs follow that style, so the replacements sit beside the game's own icons: the same body sizes, the
symbol cut out, the edge antialiased by 8 x 8 supersampling. Every shape is drawn here; nothing is copied from another
icon set.
"""

import math

import numpy as np

SS = 8   # supersampling per pixel axis


def _grid(w, h):
    ys, xs = np.mgrid[0:h * SS, 0:w * SS].astype(np.float32)
    return (xs + 0.5) / SS, (ys + 0.5) / SS


def _seg_dist(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy or 1e-9
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / l2, 0, 1)
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _arc(cx, cy, rx, ry, a0, a1, n=12):
    return [(cx + rx * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + ry * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


# Stroke letters in a unit box: x from 0 to the advance, y from 0 (top) to 1 (bottom); each a list of polylines.
LETTERS = {
    'A': (1.0, [[(0, 1), (0.5, 0), (1, 1)], [(0.22, 0.64), (0.78, 0.64)]]),
    'B': (0.95, [[(0, 0), (0, 1)], [(0, 0), (0.45, 0)] + _arc(0.45, 0.25, 0.42, 0.25, -90, 90) + [(0, 0.5)],
                 [(0, 0.5), (0.5, 0.5)] + _arc(0.5, 0.75, 0.45, 0.25, -90, 90) + [(0, 1)]]),
    'L': (0.75, [[(0, 0), (0, 1), (0.8, 1)]]),
    'R': (0.95, [[(0, 1), (0, 0), (0.5, 0)] + _arc(0.5, 0.27, 0.42, 0.27, -90, 90) + [(0, 0.54)],
                 [(0.42, 0.54), (0.95, 1)]]),
    'T': (0.95, [[(0, 0), (0.95, 0)], [(0.475, 0), (0.475, 1)]]),
    '1': (0.6, [[(0.05, 0.22), (0.5, 0), (0.5, 1)]]),
    '2': (0.95, [_arc(0.48, 0.3, 0.44, 0.3, 190, 360) + [(0.92, 0.3), (0.05, 1), (0.95, 1)]]),
}


def _text_polylines(text, cx, cy, height, gap=0.25):
    widths = [LETTERS[ch][0] for ch in text]
    x = cx - (sum(widths) + gap * (len(text) - 1)) * height / 2
    out = []
    for ch, w in zip(text, widths):
        for pl in LETTERS[ch][1]:
            out.append([(x + px * height, cy - height / 2 + py * height) for px, py in pl])
        x += (w + gap) * height
    return out


def _strokes(px, py, polylines, width):
    d = np.full(px.shape, np.inf, np.float32)
    for pl in polylines:
        for a, b in zip(pl, pl[1:]):
            d = np.minimum(d, _seg_dist(px, py, a, b))
    return d <= width / 2


def _rounded_rect(px, py, x0, y0, x1, y1, r_top, r_bottom):
    inside = (px >= x0) & (px <= x1) & (py >= y0) & (py <= y1)
    for cx, cy, r, top in ((x0 + r_top, y0 + r_top, r_top, True), (x1 - r_top, y0 + r_top, r_top, True),
                           (x0 + r_bottom, y1 - r_bottom, r_bottom, False),
                           (x1 - r_bottom, y1 - r_bottom, r_bottom, False)):
        if r > 0:
            corner = ((px < cx) if cx < (x0 + x1) / 2 else (px > cx)) & ((py < cy) if top else (py > cy))
            inside &= ~(corner & (np.hypot(px - cx, py - cy) > r))
    return inside


# ---- bodies, centred on (cx, cy), the sizes of the game's own icons. grow widens the outline by that many pixels on
# every side (the colour layer, render_icon).
def circle(cx, cy, d=22.0, grow=0.0):
    return lambda px, py: np.hypot(px - cx, py - cy) <= d / 2 + grow


def bumper(cx, cy, w=24.0, h=16.0, grow=0.0):
    """LB, RB, L1, R1: a rounded box, as the game's SL and SR."""
    return lambda px, py: _rounded_rect(px, py, cx - w / 2 - grow, cy - h / 2 - grow, cx + w / 2 + grow,
                                        cy + h / 2 + grow, 5.0 + grow, 5.0 + grow)


def trigger(cx, cy, w=24.0, h=22.0, grow=0.0):
    """LT, RT, L2, R2: a box rounded more at the top, as the game's ZL and ZR."""
    return lambda px, py: _rounded_rect(px, py, cx - w / 2 - grow, cy - h / 2 - grow, cx + w / 2 + grow,
                                        cy + h / 2 + grow, 7.0 + grow, 2.0 + grow)


# ---- cut-outs
def text(s, cx, cy, height=8.5, stroke=2.1):
    pls = _text_polylines(s, cx, cy, height)
    return lambda px, py: _strokes(px, py, pls, stroke)


def lines(cx, cy, n=3, length=10.0, gap=3.6, stroke=2.0, vertical=False):
    """Xbox Menu and PlayStation Options (three bars), PlayStation Create (three upright bars)."""
    pls = []
    for i in range(n):
        o = (i - (n - 1) / 2) * gap
        pls.append([(cx + o, cy - length / 2), (cx + o, cy + length / 2)] if vertical else
                   [(cx - length / 2, cy + o), (cx + length / 2, cy + o)])
    return lambda px, py: _strokes(px, py, pls, stroke)


def view(cx, cy, s=7.0, stroke=1.8):
    """Xbox View: two overlapping squares, the front one down and to the right."""
    o = 1.8
    bx0, by0 = cx - s / 2 + o, cy - s / 2 + o

    def sq(x, y):
        return [(x, y), (x + s, y), (x + s, y + s), (x, y + s), (x, y)]
    back, front = sq(cx - s / 2 - o, cy - s / 2 - o), sq(bx0, by0)

    def cut(px, py):
        hidden = (px > bx0) & (px < bx0 + s) & (py > by0) & (py < by0 + s)
        return (_strokes(px, py, [back], stroke) & ~hidden) | _strokes(px, py, [front], stroke)
    return cut


def ps_symbol(sym, cx, cy, size=11.0, stroke=2.2):
    """The PlayStation face symbols: cross, circle, square, triangle."""
    h = size / 2
    if sym == 'cross':
        k = h * 0.8
        pls = [[(cx - k, cy - k), (cx + k, cy + k)], [(cx - k, cy + k), (cx + k, cy - k)]]
    elif sym == 'circle':
        pls = [_arc(cx, cy, h * 0.82, h * 0.82, 0, 360, 32)]
    elif sym == 'square':
        s = h * 0.75
        pls = [[(cx - s, cy - s), (cx + s, cy - s), (cx + s, cy + s), (cx - s, cy + s), (cx - s, cy - s)]]
    elif sym == 'triangle':
        r = h * 1.1
        pts = [(cx + r * math.cos(math.radians(a)), cy + 0.15 * h + r * math.sin(math.radians(a)))
               for a in (-90, 30, 150)]
        pls = [pts + [pts[0]]]
    else:
        raise ValueError(sym)
    return lambda px, py: _strokes(px, py, pls, stroke)


def render(w, h, body, cut):
    """Coverage (h x w floats, 0..1) of body minus cut over a w x h pixel box."""
    px, py = _grid(w, h)
    m = body(px, py) & ~cut(px, py)
    return m.reshape(h, SS, w, SS).mean(axis=(1, 3))


# The game's icons keep two shapes: alpha is the body with the symbol cut out, the colour is the whole body grown by
# about 2.25 pixels (measured on the A icon: half coverage at 2.25 pixels past the alpha's), symbol not cut. The UI
# shows that colour layer as the thin grey rim around each icon and the grey inside its symbol.
RIM = 2.25


def render_icon(w, h, body, cut, cx, cy):
    """(alpha, colour) coverage pair of an icon whose body constructor is body(cx, cy, grow=...)."""
    alpha = render(w, h, body(cx, cy), cut)
    px, py = _grid(w, h)
    colour = body(cx, cy, grow=RIM)(px, py).reshape(h, SS, w, SS).mean(axis=(1, 3))
    return alpha, colour
