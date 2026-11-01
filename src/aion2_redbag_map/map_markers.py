"""Small, high-contrast map symbols rendered at the display's native size.

These images are UI artwork only. They contain no location, facing or game
state. Callers retain their own PhotoImage/cache objects for Tk.
"""
from __future__ import annotations

from PIL import Image, ImageDraw


MARKER_COLORS = {
    "seen": "#ef6b60",
    "used": "#f1b557",
    "opened": "#91a5a0",
    "soon": "#b69aeb",
    "unknown": "#9aa8ba",
    "candidate": "#8cbed1",
    "player": "#71deed",
}
OUTLINE = "#13212f"
HIGHLIGHT = "#f3f6f8"
SELECTION = "#ffe4b8"


def marker_image(kind: str, size: int = 22, selected: bool = False) -> Image.Image:
    """Return a transparent RGBA marker, with anti-aliased edges and crisp fill.

    ``size`` is the final physical pixel size, not the logical/DPI size. The
    caller should multiply its desired logical size by the display scale.
    ``player`` deliberately uses a nondirectional target; it does not imply a
    measured character heading. Unknown kinds and sizes outside 8..96 fail
    explicitly instead of producing a misleading status symbol.
    """
    if kind not in MARKER_COLORS:
        raise ValueError(f"unknown marker kind: {kind!r}")
    if type(size) is not int or not 8 <= size <= 96:
        raise ValueError("marker size must be an integer from 8 to 96")
    supersample = 4
    image = Image.new("RGBA", (size * supersample, size * supersample))
    draw = ImageDraw.Draw(image)
    unit = size * supersample / 32

    def box(values):
        return tuple(round(value * unit) for value in values)

    def width(value):
        return max(1, round(value * unit))

    def line(points, color, thickness):
        draw.line([(round(x * unit), round(y * unit)) for x, y in points],
                  fill=color, width=width(thickness), joint="curve")

    def ellipse(bounds, fill=None, outline=None, thickness=1):
        draw.ellipse(box(bounds), fill=fill, outline=outline, width=width(thickness))

    def rounded(bounds, radius, fill=None, outline=None, thickness=1):
        draw.rounded_rectangle(box(bounds), radius=width(radius), fill=fill,
                               outline=outline, width=width(thickness))

    color = MARKER_COLORS[kind]
    if selected:
        # A narrow selection ring remains legible without a soft glow or halo.
        ellipse((.7, .7, 30.3, 30.3), outline=OUTLINE, thickness=2.5)
        ellipse((1.5, 1.5, 29.5, 29.5), outline=SELECTION, thickness=1.35)

    if kind == "candidate":
        points = [(15.5, 4), (27, 15.5), (15.5, 27), (4, 15.5), (15.5, 4)]
        line(points, OUTLINE, 5)
        line(points, color, 2.5)
        ellipse((13.6, 13.6, 17.4, 17.4), fill=color)
    elif kind == "player":
        ellipse((4, 4, 27, 27), fill=OUTLINE, outline=HIGHLIGHT, thickness=1.3)
        ellipse((6.5, 6.5, 24.5, 24.5), outline=color, thickness=2.6)
        ellipse((11, 11, 20, 20), fill=color)
    elif kind == "opened":
        ellipse((4, 4, 27, 27), fill=OUTLINE, outline=color, thickness=2.2)
        line([(9.5, 15.5), (14, 20), (22, 11)], color, 3.2)
    elif kind == "unknown":
        ellipse((4, 4, 27, 27), fill=OUTLINE, outline=color, thickness=2.2)
        # Neutral three-dot state, distinct from an unopened or opened bag.
        for center in (10.5, 15.5, 20.5):
            ellipse((center - 1.2, 14.3, center + 1.2, 16.7), fill=color)
    elif kind == "soon":
        ellipse((4, 4, 27, 27), fill=color, outline=OUTLINE, thickness=2.4)
        ellipse((7, 7, 24, 24), outline=HIGHLIGHT, thickness=1.1)
        line([(15.5, 9.7), (15.5, 15.5), (19.5, 18)], OUTLINE, 2.5)
        ellipse((14.1, 14.1, 16.9, 16.9), fill=OUTLINE)
    else:
        # Handle, body and a zip pocket read as a bag even at small map scales.
        rounded((10, 3, 21, 13), 4, outline=OUTLINE, thickness=3.6)
        rounded((11.4, 4.3, 19.6, 12), 2.6, outline=HIGHLIGHT, thickness=1.6)
        rounded((4, 8, 27, 28), 5, fill=color, outline=OUTLINE, thickness=2.3)
        line([(9, 11), (22, 11)], HIGHLIGHT, 1.1)
        rounded((8.5, 17, 22.5, 24.5), 2.2, outline=OUTLINE, thickness=1.6)
        line([(10, 19.2), (21, 19.2)], OUTLINE, 1.3)
        line([(19, 19.2), (19, 21.5)], HIGHLIGHT, 1.2)

    image = image.resize((size, size), Image.Resampling.LANCZOS)
    # Remove near-invisible Lanczos ringing around tiny icons. Edges retain
    # their real partial coverage, while transparent corners stay transparent.
    image.putalpha(image.getchannel("A").point(lambda alpha: 0 if alpha <= 3 else alpha))
    return image
