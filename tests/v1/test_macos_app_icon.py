from pathlib import Path

from PIL import Image, ImageChops, ImageStat


ROOT = Path(__file__).resolve().parents[2]


def test_macos_icon_uses_the_native_rounded_silhouette_without_redrawing_brand() -> None:
    mac = Image.open(ROOT / "desktop/build/icon.icns").convert("RGBA")
    windows = Image.open(ROOT / "desktop/build/icon.ico").ico.getimage((256, 256)).convert("RGBA")

    alpha = mac.getchannel("A")
    assert alpha.getbbox() == (100, 100, 924, 924)
    corners = ((0, 0), (1023, 0), (0, 1023), (1023, 1023))
    assert all(alpha.getpixel(point) == 0 for point in corners)
    assert alpha.getpixel((512, 512)) == 255

    visible = mac.crop((100, 100, 924, 924)).resize((256, 256), Image.Resampling.LANCZOS)
    visible = Image.alpha_composite(Image.new("RGBA", visible.size, "black"), visible)
    assert max(ImageStat.Stat(ImageChops.difference(visible, windows)).mean[:3]) < 1.0
