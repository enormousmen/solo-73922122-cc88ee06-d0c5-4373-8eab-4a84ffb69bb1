"""生成两张内置源图：常规底图 / 应急航片（2048x2048 PNG）。"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = 2048


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:  # 旧版 Pillow 兜底
        return ImageFont.load_default()


def _gradient(img: Image.Image, c1, c2) -> None:
    d = ImageDraw.Draw(img)
    for i in range(SIZE):
        t = i / (SIZE - 1)
        d.line([(0, i), (SIZE, i)], fill=tuple(int(a + (b - a) * t) for a, b in zip(c1, c2)))


def _annotate(img: Image.Image, title: str, subtitle: str, accent) -> None:
    d = ImageDraw.Draw(img)
    for i in range(0, SIZE + 1, 256):  # 瓦片边界网格，便于观察切片对齐
        d.line([(i, 0), (i, SIZE)], fill=(255, 255, 255), width=1)
        d.line([(0, i), (SIZE, i)], fill=(255, 255, 255), width=1)
    f_small = _font(18)
    for gx in range(8):
        for gy in range(8):
            d.text((gx * 256 + 6, gy * 256 + 4), f"{gx},{gy}", font=f_small, fill=(30, 30, 30))
    d.rectangle([SIZE // 2 - 520, SIZE // 2 - 110, SIZE // 2 + 520, SIZE // 2 + 110],
                fill=(255, 255, 255), outline=accent, width=6)
    d.text((SIZE // 2, SIZE // 2 - 40), title, font=_font(96), fill=accent, anchor="mm")
    d.text((SIZE // 2, SIZE // 2 + 50), subtitle, font=_font(36), fill=(40, 40, 40), anchor="mm")


def make_regular(path: Path) -> None:
    img = Image.new("RGB", (SIZE, SIZE))
    _gradient(img, (168, 204, 235), (120, 190, 150))
    d = ImageDraw.Draw(img)
    for r in range(100, 1000, 150):
        d.ellipse([SIZE // 2 - r, SIZE // 2 - r, SIZE // 2 + r, SIZE // 2 + r],
                  outline=(255, 255, 255), width=3)
    _annotate(img, "REGULAR BASEMAP", "2048x2048 / z0-z3 / 256px", (20, 80, 140))
    img.save(path, "PNG")


def make_emergency(path: Path) -> None:
    img = Image.new("RGB", (SIZE, SIZE))
    _gradient(img, (245, 180, 120), (210, 70, 60))
    d = ImageDraw.Draw(img)
    for i in range(-SIZE, SIZE * 2, 128):
        d.line([(i, 0), (i + SIZE, SIZE)], fill=(255, 235, 200), width=6)
    _annotate(img, "EMERGENCY", "ORTHO PHOTO 2048x2048", (150, 20, 20))
    img.save(path, "PNG")


def ensure_sources(data_dir: Path):
    d = Path(data_dir) / "sources"
    d.mkdir(parents=True, exist_ok=True)
    reg, emg = d / "regular.png", d / "emergency.png"
    if not reg.is_file():
        make_regular(reg)
    if not emg.is_file():
        make_emergency(emg)
    return reg, emg
