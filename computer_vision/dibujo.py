#!/usr/bin/env python3
"""
Draw ROI polygons / line-crossing on a video frame and save to nvdsanalytics config.

Usage:
  python3 dibujo.py stream-0 [--video PATH]   # draw ROI on stream 0
  python3 dibujo.py stream-1 --line           # draw line-crossing on stream 1
"""

import argparse
import cv2
import os
import json
import re

CONFIG_FILE = "/opt/computer_vision/config/config_nvdsanalytics.txt"
DEFAULT_VIDEO_0 = "/opt/computer_vision/test/cam1_full.mp4"
DEFAULT_VIDEO_1 = "/opt/computer_vision/test/cam2_full.mp4"
FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080

points = []
drawing = False
mode = "roi"  # "roi" or "line"
line_start = None


def draw_callback(event, x, y, flags, param):
    global points, drawing, line_start

    img = param["img"]
    display = img.copy()

    if mode == "roi":
        for p in points:
            cv2.circle(display, p, 4, (0, 255, 255), -1)
        if len(points) > 1:
            cv2.polylines(display, [np.array(points, dtype=np.int32)], False, (0, 255, 0), 2)
        if len(points) > 2:
            cv2.polylines(display, [np.array(points + [points[0]], dtype=np.int32)], True, (0, 255, 0), 2)

        if event == cv2.EVENT_LBUTTONDOWN:
            points.append((x, y))

    elif mode == "line":
        for p in points:
            cv2.circle(display, p, 5, (0, 255, 255), -1)
        if len(points) >= 2:
            cv2.line(display, points[0], points[1], (0, 255, 0), 3)
            cv2.arrowedLine(display, points[0], points[1], (0, 0, 255), 2, tipLength=0.05)

        if event == cv2.EVENT_LBUTTONDOWN:
            if len(points) >= 2:
                points = []
            points.append((x, y))

    cv2.imshow("Draw — [ENTER] save  [ESC] quit  [r] reset", display)


def extract_frame(video_path):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"ERROR: Cannot open {video_path}")
        return None

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.set(cv2.CAP_PROP_POS_FRAMES, total // 3)  # ~33% into video
    ret, frame = cap.read()
    cap.release()

    if not ret:
        print(f"ERROR: Cannot read frame from {video_path}")
        return None

    h, w = frame.shape[:2]
    if w > 1200:
        scale = 1200 / w
        frame = cv2.resize(frame, (1200, int(h * scale)))
        print(f"Resized: {w}x{h} -> 1200x{int(h * scale)}")

    FRAME_WIDTH = w  # original width for coordinate scaling
    return frame, FRAME_WIDTH


def normalize_points(pts, original_width):
    """Convert display coordinates back to 1920x1080 config space."""
    return [(round(p[0] * FRAME_WIDTH / 1200), round(p[1] * FRAME_HEIGHT / (original_width * FRAME_HEIGHT / FRAME_WIDTH))) for p in pts]


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
    global mode, points, line_start

    parser = argparse.ArgumentParser(description="Draw ROI/line for nvdsanalytics config")
    parser.add_argument("stream", help="stream-0 or stream-1")
    parser.add_argument("--video", help="Video file path (default: camX_full.mp4)")
    parser.add_argument("--line", action="store_true", help="Draw line-crossing instead of ROI")
    args = parser.parse_args()

    if args.stream == "stream-0":
        video_path = args.video or DEFAULT_VIDEO_0
        stream_idx = 0
    elif args.stream == "stream-1":
        video_path = args.video or DEFAULT_VIDEO_1
        stream_idx = 1
    else:
        print("Usage: python3 dibujo.py stream-0|stream-1 [--line] [--video PATH]")
        return

    mode = "line" if args.line else "roi"

    result = extract_frame(video_path)
    if result is None:
        return
    frame, orig_w = result

    import numpy as np

    cv2.namedWindow("Draw — [ENTER] save  [ESC] quit  [r] reset")
    cv2.imshow("Draw — [ENTER] save  [ESC] quit  [r] reset", frame)
    cv2.waitKey(1)
    cv2.setMouseCallback("Draw — [ENTER] save  [ESC] quit  [r] reset", draw_callback, {"img": frame})

    print(f"Mode: {mode.upper()}")
    print("Click to add points. [ENTER] to save, [ESC] to quit, [r] to reset")

    while True:
        display = frame.copy()
        if mode == "roi":
            for p in points:
                cv2.circle(display, p, 4, (0, 255, 255), -1)
            if len(points) > 1:
                cv2.polylines(display, [np.array(points, dtype=np.int32)], False, (0, 255, 0), 2)
            if len(points) > 2:
                cv2.polylines(display, [np.array(points + [points[0]], dtype=np.int32)], True, (0, 255, 0), 2)
        else:
            for p in points:
                cv2.circle(display, p, 5, (0, 255, 255), -1)
            if len(points) >= 2:
                cv2.line(display, points[0], points[1], (0, 255, 0), 3)
                cv2.arrowedLine(display, points[0], points[1], (0, 0, 255), 2, tipLength=0.05)
        cv2.imshow("Draw — [ENTER] save  [ESC] quit  [r] reset", display)

        key = cv2.waitKey(50) & 0xFF
        if key == 13:  # Enter
            sections, _ = read_config()

            if mode == "roi":
                if len(points) < 3:
                    print("Need at least 3 points for a polygon")
                    continue
                scaled = normalize_points(points, orig_w)
                coords = ";".join(f"{p[0]};{p[1]}" for p in scaled)
                name = input("ROI name: ").strip() or "ROI-" + str(len(points))
                section = f"roi-filtering-stream-{stream_idx}"
                sections[section] = {"enable": "1", "class-id": "-1", f"roi-{name}": coords}
                print(f"ROI '{name}' saved to [{section}]")
            else:
                if len(points) < 2:
                    print("Need 2 points for a line")
                    continue
                scaled = normalize_points(points[:2], orig_w)
                coords = f"{scaled[0][0]};{scaled[0][1]};{scaled[1][0]};{scaled[1][1]}"
                name = input("Line name: ").strip() or "LC-" + str(stream_idx)
                section = f"line-crossing-stream-{stream_idx}"
                sections[section] = {"enable": "1", "class-id": "4", "mode": "1", f"line-crossing-{name}": coords}
                print(f"Line '{name}' saved to [{section}]")

            write_config(sections)
            points = []
            break

        elif key == 27:  # ESC
            break
        elif key == ord("r"):
            points = []
            print("Reset")

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
