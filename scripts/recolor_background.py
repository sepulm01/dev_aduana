import argparse
import sys
from pathlib import Path

from PIL import Image


def recolor_dark_image(
    src: Path,
    dst: Path,
    target_color: tuple[int, int, int] = (17, 24, 39),
    threshold: int = 60,
    fade_zone: int = 40,
) -> None:
    img = Image.open(src).convert("RGBA")
    pixels = img.load()
    width, height = img.size

    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a == 0:
                continue
            lightness = (int(r) + int(g) + int(b)) / 3
            if lightness <= threshold:
                factor = 1.0
            elif lightness <= threshold + fade_zone:
                factor = 1.0 - (lightness - threshold) / fade_zone
            else:
                factor = 0.0
            if factor > 0:
                r = int(r * (1 - factor) + target_color[0] * factor)
                g = int(g * (1 - factor) + target_color[1] * factor)
                b = int(b * (1 - factor) + target_color[2] * factor)
                pixels[x, y] = (r, g, b, a)

    img.save(dst, "PNG")


def main():
    parser = argparse.ArgumentParser(description="Recolor a dark image to match a theme")
    parser.add_argument("source", type=Path, help="Source image path")
    parser.add_argument("--output", "-o", type=Path, default=None, help="Output path")
    parser.add_argument(
        "--color", "-c", type=str, default="111827", help="Target hex color (no #)"
    )
    parser.add_argument(
        "--threshold", "-t", type=int, default=60, help="Lightness threshold (0-255)"
    )
    args = parser.parse_args()

    if not args.source.exists():
        print(f"Error: {args.source} not found", file=sys.stderr)
        sys.exit(1)

    dst = args.output or args.source.with_stem(args.source.stem + "_recolored")
    target = tuple(int(args.color[i : i + 2], 16) for i in (0, 2, 4))
    recolor_dark_image(args.source, dst, target, args.threshold)
    print(f"Saved: {dst}")


if __name__ == "__main__":
    main()
