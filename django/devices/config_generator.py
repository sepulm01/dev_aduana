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
    return "con_sello;cont data;sin_sello;container cod;"


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
    path = os.path.join(config_dir, NVDSANALYTICS_CONFIG_FILE)
    with open(path, "w") as f:
        f.write(
            "[property]\n"
            "enable=0\n"
        )
