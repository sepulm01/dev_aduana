import os


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

osd:
  process-mode: 0
  display-text: 1

tiler:
  width: 1280
  height: 720

sink:
  qos: 0
"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(config)

    return uris
