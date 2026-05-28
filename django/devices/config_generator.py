import os

NVDSANALYTICS_CONFIG_FILE = "config_nvdsanalytics.txt"
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720


def _shapes_to_nvdsanalytics(shapes, stream_idx=0, prefix=""):
    sections = {}
    for shape in shapes:
        obj_type = shape.get("object", "")
        name = shape.get("name", "unnamed")
        shape_type = shape.get("type", "")

        if obj_type == "polygon" and shape_type == "RF":
            pts = shape.get("points", [])
            if len(pts) >= 4:
                coords = ";".join(
                    f"{round(p['x'] * FRAME_WIDTH)};{round(p['y'] * FRAME_HEIGHT)}"
                    for p in pts
                )
                key = f"roi-{name}" if not prefix else f"{prefix}_roi-{name}"
                section = f"roi-filtering-stream-{stream_idx}"
                if section not in sections:
                    sections[section] = {"enable": "1", "class-id": "-1"}
                sections[section][key] = coords

        elif obj_type == "polygon" and shape_type == "OC":
            pts = shape.get("points", [])
            if len(pts) >= 4:
                coords = ";".join(
                    f"{round(p['x'] * FRAME_WIDTH)};{round(p['y'] * FRAME_HEIGHT)}"
                    for p in pts
                )
                key = f"roi-{name}" if not prefix else f"{prefix}_roi-{name}"
                section = f"overcrowding-stream-{stream_idx}"
                if section not in sections:
                    sections[section] = {
                        "enable": "1", "class-id": "-1", "object-threshold": "3"
                    }
                sections[section][key] = coords

        elif obj_type == "line" and shape_type == "cross":
            x1 = round(shape["x1"] * FRAME_WIDTH)
            y1 = round(shape["y1"] * FRAME_HEIGHT)
            x2 = round(shape["x2"] * FRAME_WIDTH)
            y2 = round(shape["y2"] * FRAME_HEIGHT)
            key = f"line-crossing-{name}" if not prefix else f"{prefix}_line-crossing-{name}"
            section = f"line-crossing-stream-{stream_idx}"
            if section not in sections:
                sections[section] = {"enable": "1", "class-id": "0", "mode": "loose"}
            sections[section][key] = f"{x1};{y1};{x2};{y2}"

        elif obj_type == "line" and shape_type == "direction":
            x1 = round(shape["x1"] * FRAME_WIDTH)
            y1 = round(shape["y1"] * FRAME_HEIGHT)
            x2 = round(shape["x2"] * FRAME_WIDTH)
            y2 = round(shape["y2"] * FRAME_HEIGHT)
            key = f"direction-{name}" if not prefix else f"{prefix}_direction-{name}"
            section = f"direction-detection-stream-{stream_idx}"
            if section not in sections:
                sections[section] = {"enable": "1", "class-id": "0"}
            sections[section][key] = f"{x1};{y1};{x2};{y2}"

    return sections


def _serialize_nvdsanalytics(sections):
    lines = [
        "[property]",
        "enable=1",
        "config-width=1280",
        "config-height=720",
        "osd-mode=1",
        "",
    ]
    for section, props in sections.items():
        lines.append(f"[{section}]")
        for key, val in props.items():
            lines.append(f"{key}={val}")
        lines.append("")
    return "\n".join(lines)


def generate_nvdsanalytics_config(devices, config_dir):
    from django.apps import apps

    AnalyticsPreset = apps.get_model("devices", "AnalyticsPreset")

    all_sections = {}
    stream_idx = 0
    for device in devices:
        presets = AnalyticsPreset.objects.filter(
            device=device, shapes__isnull=False
        ).exclude(shapes=[])
        if not presets:
            stream_idx += 1
            continue
        for preset in presets:
            prefix = preset.preset_token if preset.preset_token != "__fixed__" else ""
            sections = _shapes_to_nvdsanalytics(preset.shapes, stream_idx, prefix)
            for sec, props in sections.items():
                if sec not in all_sections:
                    all_sections[sec] = dict(props)
                else:
                    for k, v in props.items():
                        if k not in ("enable", "class-id", "mode", "object-threshold"):
                            all_sections[sec][k] = v
        stream_idx += 1

    content = _serialize_nvdsanalytics(all_sections)
    output_path = os.path.join(config_dir, NVDSANALYTICS_CONFIG_FILE)
    os.makedirs(config_dir, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(content)


def generate_config(devices, output_path, models_dir=None):
    uris = []
    for device in devices:
        if not device.is_online or not device.stream_uris:
            continue
        if not device.default_profile_token:
            continue
        uri = device.stream_uris.get(device.default_profile_token, "")
        if not uri:
            continue
        uris.append(uri)

    source_list = ";".join(uris) + ";" if uris else ""
    batch_size = len(uris) or 1

    if models_dir is None:
        models_dir = os.environ.get("MODELS_DIR", "../models/peoplenet")

    config_dir = os.path.dirname(output_path)

    config = f"""source-list:
  list: "{source_list}"

streammux:
  batch-size: {batch_size}
  batched-push-timeout: 40000
  width: 1920
  height: 1080

primary-gie:
  plugin-type: 0
  config-file-path: {models_dir}/pgie_config.yml

analytics:
  enable: 1
  config-file: {NVDSANALYTICS_CONFIG_FILE}

osd:
  process-mode: 0
  display-text: 1

tiler:
  width: 1280
  height: 720

sink:
  qos: 0
"""

    os.makedirs(config_dir, exist_ok=True)
    with open(output_path, "w") as f:
        f.write(config)

    return uris
