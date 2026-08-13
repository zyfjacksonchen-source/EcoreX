from pathlib import Path

from PIL import Image, ImageChops, ImageStat


ROOT = Path(__file__).resolve().parents[2]


def test_desktop_icons_share_the_native_rounded_silhouette_without_redrawing_brand() -> None:
    mac = Image.open(ROOT / "desktop/build/icon.icns").convert("RGBA")
    windows = Image.open(ROOT / "desktop/build/icon.ico").ico.getimage((256, 256)).convert("RGBA")

    for icon, bounds in ((mac, (100, 100, 924, 924)), (windows, (25, 25, 231, 231))):
        alpha = icon.getchannel("A")
        assert alpha.getbbox() == bounds
        width, height = icon.size
        corners = ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
        assert all(alpha.getpixel(point) == 0 for point in corners)
        assert alpha.getpixel((width // 2, height // 2)) == 255

    mac_visible = mac.crop((100, 100, 924, 924)).resize((256, 256), Image.Resampling.LANCZOS)
    win_visible = windows.crop((25, 25, 231, 231)).resize((256, 256), Image.Resampling.LANCZOS)
    mac_visible = Image.alpha_composite(Image.new("RGBA", mac_visible.size, "black"), mac_visible)
    win_visible = Image.alpha_composite(Image.new("RGBA", win_visible.size, "black"), win_visible)
    assert max(ImageStat.Stat(ImageChops.difference(mac_visible, win_visible)).mean[:3]) < 3.0
