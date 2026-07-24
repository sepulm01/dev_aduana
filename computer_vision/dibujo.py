#!/usr/bin/env python3
"""
Draw ROI polygons or line-crossing on a camera frame using matplotlib.
Saves to /opt/computer_vision/config/config_nvdsanalytics.txt.

Usage:
  python3 dibujo.py stream-0            # draw polygon ROI
  python3 dibujo.py stream-0 --line     # draw line-crossing
"""

import argparse
import os
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backend_bases import MouseButton
from matplotlib.patches import Polygon

CONFIG_FILE = "/opt/computer_vision/config/config_nvdsanalytics.txt"
FRAME_0 = "/opt/computer_vision/test/cam1_frame.jpg"
FRAME_1 = "/opt/computer_vision/test/cam2_frame.jpg"
FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080

points = []
mode = "roi"
ax = None
poly_patch = None
line_art = None
text_ann = None
fig = None
stream_idx = 0
display_width = 1200
scale = 1.0


def redraw():
    global poly_patch, line_art, text_ann

    if poly_patch:
        poly_patch.remove()
        poly_patch = None
    if line_art:
        line_art.remove()
        line_art = None
    if text_ann:
        text_ann.remove()
        text_ann = None

    if mode == "roi" and len(points) >= 2:
        pts = points
        if len(pts) > 2:
            poly_patch = Polygon(pts, closed=True, fill=True, alpha=0.3,
                                 facecolor="green", edgecolor="lime", linewidth=2)
        else:
            poly_patch, = ax.plot([p[0] for p in pts], [p[1] for p in pts], "yo-", lw=2, ms=6)
        ax.add_patch(poly_patch) if isinstance(poly_patch, Polygon) else None

    if mode == "line" and len(points) >= 2:
        xs = [points[0][0], points[1][0]]
        ys = [points[0][1], points[1][1]]
        line_art, = ax.plot(xs, ys, "lime", lw=3)
        ax.annotate("", xy=(xs[1], ys[1]), xytext=(xs[0], ys[0]),
                    arrowprops=dict(arrowstyle="->", color="red", lw=2))

    for x, y in points:
        dot, = ax.plot(x, y, "yo", ms=5)
        if not hasattr(redraw, "dots"):
            redraw.dots = []
        redraw.dots.append(dot)

    fig.canvas.draw_idle()


def onclick(event):
    global points
    if event.inaxes != ax:
        return
    if event.button != MouseButton.LEFT:
        return

    points.append((event.xdata, event.ydata))
    redraw()


def onkey(event):
    global points

    if event.key == "enter":
        if mode == "roi" and len(points) < 3:
            print("Need at least 3 points for a polygon")
            return
        if mode == "line" and len(points) < 2:
            print("Need 2 points for a line")
            return

        sections, _ = read_config()

        if mode == "roi":
            scaled = [(round(x * FRAME_WIDTH / display_width),
                       round(y * FRAME_HEIGHT / (display_width * FRAME_HEIGHT / FRAME_WIDTH)))
                      for x, y in points]
            coords = ";".join(f"{p[0]};{p[1]}" for p in scaled)
            name = input("ROI name: ").strip() or "ROI-stream-" + str(stream_idx)
            section = f"roi-filtering-stream-{stream_idx}"
            sections[section] = {"enable": "1", "class-id": "-1", f"roi-{name}": coords}
            print(f"ROI '{name}' saved to [{section}]")
        else:
            scaled = [(round(x * FRAME_WIDTH / display_width),
                       round(y * FRAME_HEIGHT / (display_width * FRAME_HEIGHT / FRAME_WIDTH)))
                      for x, y in points[:2]]
            coords = f"{scaled[0][0]};{scaled[0][1]};{scaled[1][0]};{scaled[1][1]}"
            name = input("Line name: ").strip() or "LC-stream-" + str(stream_idx)
            section = f"line-crossing-stream-{stream_idx}"
            sections[section] = {"enable": "1", "class-id": "4", "mode": "1", f"line-crossing-{name}": coords}
            print(f"Line '{name}' saved to [{section}]")

        write_config(sections)
        points = []
        redraw()

    elif event.key == "escape":
        plt.close()

    elif event.key == "r":
        points = []
        print("Reset")
        redraw()


def read_config():
    if not os.path.exists(CONFIG_FILE):
        return {}, ""
    with open(CONFIG_FILE) as f:
        content = f.read()
    sections = {}
    current = "property"
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections[current] = {}
        elif "=" in line:
            k, v = line.split("=", 1)
            sections.setdefault(current, {})[k] = v
    return sections, content


def write_config(sections):
    lines = []
    for sec_name, props in sections.items():
        lines.append(f"[{sec_name}]")
        for k, v in props.items():
            lines.append(f"{k}={v}")
        lines.append("")
    with open(CONFIG_FILE, "w") as f:
        f.write("\n".join(lines))
    print(f"Saved to {CONFIG_FILE}")


def main():
    global mode, points, ax, fig, stream_idx, display_width, scale

    parser = argparse.ArgumentParser(description="Draw ROI/line for nvdsanalytics config")
    parser.add_argument("stream", help="stream-0 or stream-1")
    parser.add_argument("--line", action="store_true", help="Draw line-crossing instead of ROI")
    args = parser.parse_args()

    if args.stream == "stream-0":
        frame_path = FRAME_0
        stream_idx = 0
    elif args.stream == "stream-1":
        frame_path = FRAME_1
        stream_idx = 1
    else:
        print("Usage: python3 dibujo.py stream-0|stream-1 [--line]")
        return

    mode = "line" if args.line else "roi"

    if not os.path.exists(frame_path):
        print(f"ERROR: Frame not found at {frame_path}")
        print("Extract frames first:")
        print("  ffmpeg -i /opt/computer_vision/test/cam1_full.mp4 -ss 10 -vframes 1 /opt/computer_vision/test/cam1_frame.jpg")
        return

    img = plt.imread(frame_path)
    h, w = img.shape[:2]
    display_width = 1200
    scale = display_width / w
    display_height = int(h * scale)

    fig, ax = plt.subplots(figsize=(display_width / 100, display_height / 100), dpi=100)
    ax.imshow(img, extent=[0, display_width, display_height, 0])
    ax.set_xlim(0, display_width)
    ax.set_ylim(display_height, 0)
    ax.set_title(f"stream-{stream_idx} — {mode.upper()}  [click=add] [enter=save] [r=reset] [esc=quit]")
    ax.axis("off")

    fig.canvas.mpl_connect("button_press_event", onclick)
    fig.canvas.mpl_connect("key_press_event", onkey)

    print(f"Mode: {mode.upper()}  |  Stream: {stream_idx}")
    print("Click to add points. [ENTER] save, [r] reset, [ESC] quit")
    plt.show()


if __name__ == "__main__":
    main()
