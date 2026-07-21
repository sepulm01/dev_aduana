import os

NVDSANALYTICS_CONFIG_FILE = "config_nvdsanalytics.txt"
FRAME_WIDTH = 1280
FRAME_HEIGHT = 720

MAX_INSTANCES = 1

PIPELINE_CONFIGS = {
    "aduana": {
        "models_dir": "../models/yolov9_aduana",
        "max_streammux_batch": 2,
        "max_devices_per_instance": 2,
        "filename_template": "aduana",
    },
}

PIPELINE_CONTAINER_SUFFIX = {
    "aduana": "computer-vision-aduana",
}


def get_pipeline_filename(pipeline_id, instance=1):
    basename = PIPELINE_CONFIGS[pipeline_id].get("filename_template", pipeline_id)
    if instance == 1:
        return f"config_{basename}.yml"
    return f"config_{basename}_{instance}.yml"


def _read_labels(pipeline_id, config_dir):
    pipeline_cfg = PIPELINE_CONFIGS[pipeline_id]
    models_dir = pipeline_cfg.get("models_dir", "../models/yolov9_aduana")
    labels_path = os.path.join(
        config_dir, models_dir, "labels.txt"
    )
    if os.path.exists(labels_path):
        with open(labels_path) as f:
            names = [line.strip() for line in f if line.strip()]
            return ";".join(names) + ";"
    return "con_sello;sin_sello;cont data;container cod;truck;"


def generate_config(devices, output_path, pipeline_id="aduana"):
    uris = []
    for device in devices:
        if not device.stream_uris:
            continue
        if device.source_type == "rtsp" and not device.is_online:
            continue
        if not device.default_profile_token:
            continue
        uri = device.stream_uris.get(device.default_profile_token, "")
        if not uri:
            continue
        if device.source_type == "file":
            token = device.default_profile_token
            uri = f"rtsp://mediamtx:8554/cam_{device.id}_{token}"
        uris.append(uri)

    source_list = ";".join(uris) + ";" if uris else ""
    raw_batch_size = len(uris) or 1

    pipeline_cfg = PIPELINE_CONFIGS[pipeline_id]
    max_batch = pipeline_cfg.get("max_streammux_batch", raw_batch_size)
    batch_size = min(raw_batch_size, max_batch)

    models_dir = pipeline_cfg.get("models_dir", "../models/yolov9_aduana")

    config_dir = os.path.dirname(output_path)
    os.makedirs(config_dir, exist_ok=True)

    labels = _read_labels(pipeline_id, config_dir)

    config = f"""source-list:
  list: "{source_list}"

streammux:
  batch-size: {batch_size}
  batched-push-timeout: 40000
  width: 1920
  height: 1080
  live-source: 1
  attach-sys-ts: 0
  sync-inputs: 0

labels: {labels}

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

    with open(output_path, "w") as f:
        f.write(config)

    return uris


def generate_all_configs(config_dir=None):
    from django.apps import apps

    Device = apps.get_model("devices", "Device")

    if config_dir is None:
        config_dir = os.path.dirname(
            os.environ.get("CONFIG_YML_PATH", "/opt/computer_vision/config/config_aduana.yml")
        )

    for pipeline_id in PIPELINE_CONFIGS:
        devices = list(
            Device.objects.filter(
                deepstream_pipeline=pipeline_id,
                is_online=True,
                stream_uris__isnull=False,
                source_type="rtsp",
            ).exclude(stream_uris={})
        )
        devices += list(
            Device.objects.filter(
                deepstream_pipeline=pipeline_id,
                stream_uris__isnull=False,
                source_type="file",
            ).exclude(stream_uris={})
        )

        pipeline_cfg = PIPELINE_CONFIGS[pipeline_id]
        max_per_instance = pipeline_cfg["max_devices_per_instance"]

        for n in range(1, MAX_INSTANCES + 1):
            filename = get_pipeline_filename(pipeline_id, instance=n)
            output_path = os.path.join(config_dir, filename)

            if devices:
                my_devices = devices[(n - 1) :: MAX_INSTANCES][:max_per_instance]
                generate_config(my_devices, output_path, pipeline_id)
            else:
                with open(output_path, "w") as f:
                    f.write(f"""source-list:
  list: ""

streammux:
  batch-size: 1
  batched-push-timeout: 40000
  width: 1920
  height: 1080
  live-source: 1
  attach-sys-ts: 0
  sync-inputs: 0

labels: ;

primary-gie:
  plugin-type: 0
  config-file-path: ../models/yolov9_aduana/pgie_config.yml

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
""")

    generate_nvdsanalytics_config(config_dir)


def generate_nvdsanalytics_config(config_dir):
    from django.apps import apps

    Device = apps.get_model("devices", "Device")
    AnalyticsPreset = apps.get_model("devices", "AnalyticsPreset")

    devices = list(
        Device.objects.filter(
            is_online=True,
            stream_uris__isnull=False,
            source_type="rtsp",
        ).exclude(stream_uris={})
    )
    devices += list(
        Device.objects.filter(
            is_online=True,
            stream_uris__isnull=False,
            source_type="file",
        ).exclude(stream_uris={})
    )
    devices.sort(key=lambda d: d.host)

    sections = {}
    has_any = False

    for stream_idx, device in enumerate(devices):
        token = device.default_profile_token or "__fixed__"
        preset = AnalyticsPreset.objects.filter(
            device=device, preset_token=token
        ).first()
        if (not preset or not preset.shapes) and token != "__fixed__":
            preset = AnalyticsPreset.objects.filter(
                device=device, preset_token="__fixed__"
            ).first()
        if not preset or not preset.shapes:
            continue

        device_sections = _shapes_to_nvdsanalytics(preset.shapes, stream_idx)
        for sec_name, sec_data in device_sections.items():
            if sec_name not in sections:
                sections[sec_name] = {}
            sections[sec_name].update(sec_data)
        has_any = True

    if not has_any:
        path = os.path.join(config_dir, NVDSANALYTICS_CONFIG_FILE)
        with open(path, "w") as f:
            f.write(
                "[property]\n"
                "enable=1\n"
                f"config-width={FRAME_WIDTH}\n"
                f"config-height={FRAME_HEIGHT}\n"
                "osd-mode=1\n"
            )
        return

    path = os.path.join(config_dir, NVDSANALYTICS_CONFIG_FILE)
    with open(path, "w") as f:
        f.write(_serialize_nvdsanalytics(sections))


def _shapes_to_nvdsanalytics(shapes, stream_idx=0):
    sections = {}
    for shape in shapes:
        obj_type = shape.get("object", "")
        name = shape.get("name", "unnamed")
        shape_type = shape.get("type", "")

        if obj_type == "polygon" and shape_type == "RF":
            pts = shape.get("points", [])
            if len(pts) >= 3:
                coords = ";".join(
                    f"{round(p['x'] * FRAME_WIDTH)};{round(p['y'] * FRAME_HEIGHT)}"
                    for p in pts
                )
                key = f"roi-{name}"
                section = f"roi-filtering-stream-{stream_idx}"
                if section not in sections:
                    sections[section] = {"enable": "1", "class-id": "-1"}
                sections[section][key] = coords

        elif obj_type == "line" and shape_type == "cross":
            x1 = round(shape["x1"] * FRAME_WIDTH)
            y1 = round(shape["y1"] * FRAME_HEIGHT)
            x2 = round(shape["x2"] * FRAME_WIDTH)
            y2 = round(shape["y2"] * FRAME_HEIGHT)
            key = f"line-crossing-{name}"
            section = f"line-crossing-stream-{stream_idx}"
            if section not in sections:
                sections[section] = {"enable": "1", "class-id": "0", "mode": "balanced"}
            sections[section][key] = f"{x1};{y1};{x2};{y2}"

    return sections


def _serialize_nvdsanalytics(sections):
    lines = [
        "[property]",
        "enable=1",
        f"config-width={FRAME_WIDTH}",
        f"config-height={FRAME_HEIGHT}",
        "osd-mode=1",
        "",
    ]
    for sec_name, props in sorted(sections.items()):
        lines.append(f"[{sec_name}]")
        for key, value in sorted(props.items()):
            lines.append(f"{key}={value}")
        lines.append("")
    return "\n".join(lines) + "\n"
