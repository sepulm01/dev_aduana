class BaseNotificationBackend:
    def send(self, channel, message_text, extra_params=None):
        raise NotImplementedError

    def send_with_reply_markup(self, channel, message_text, reply_markup, extra_params=None):
        raise NotImplementedError

    def send_with_photo(self, channel, message_text, photo_bytes):
        raise NotImplementedError

    def format_message(self, template, event):
        if not template:
            return self.default_message(event)
        try:
            return template.format(**event)
        except (KeyError, ValueError):
            return self.default_message(event)

    def default_message(self, event):
        device_name = event.get("device_name", "Unknown")
        code = event.get("code", "Event")
        action = event.get("action", "")
        data = event.get("data", {})
        objects = data.get("Object", [])
        analytics = data.get("analytics", {})

        lines = [f"Dispositivo: {device_name}"]
        lines.append(f"Evento: {code} {action}".strip())

        if objects:
            labels = {}
            for obj in objects:
                label = obj.get("class_label", "object")
                labels[label] = labels.get(label, 0) + 1
            parts = [f"{v} {k}" for k, v in labels.items()]
            lines.append(f"Detecciones: {', '.join(parts)}")

            for obj in objects:
                if obj.get("roi"):
                    lines.append(f"ROI: {', '.join(obj['roi'])}")
                if obj.get("lc"):
                    lines.append(f"LC: {', '.join(obj['lc'])}")
                if obj.get("oc"):
                    lines.append(f"OC: {', '.join(obj['oc'])}")
                if obj.get("direction"):
                    lines.append(f"Direccion: {obj['direction']}")

        if analytics:
            for k, v in analytics.items():
                if isinstance(v, bool):
                    lines.append(f"{k}: {'SI' if v else 'NO'}")
                else:
                    lines.append(f"{k}: {v}")

        return "\n".join(lines)
