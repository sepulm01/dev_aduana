class PTZService:
    def __init__(self, client):
        self.client = client

    def get_status(self, profile_token):
        try:
            return self.client.ptz.GetStatus({"ProfileToken": profile_token})
        except Exception:
            return None

    def get_presets(self, profile_token):
        try:
            return self.client.ptz.GetPresets({"ProfileToken": profile_token})
        except Exception:
            return []

    def get_configuration(self, profile_token):
        try:
            return self.client.ptz.GetConfiguration({"ProfileToken": profile_token})
        except Exception:
            return None

    def absolute_move(self, profile_token, pan=0.0, tilt=0.0, zoom=0.0):
        params = {
            "ProfileToken": profile_token,
            "Position": {
                "PanTilt": {
                    "x": pan,
                    "y": tilt,
                    "space": "http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGeneric",
                },
                "Zoom": {
                    "x": zoom,
                    "space": "http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGeneric",
                },
            },
        }
        return self.client.ptz.AbsoluteMove(params)

    def continuous_move(self, profile_token, pan=0.0, tilt=0.0, zoom=0.0):
        params = {
            "ProfileToken": profile_token,
            "Velocity": {
                "PanTilt": {
                    "x": pan,
                    "y": tilt,
                    "space": "http://www.onvif.org/ver10/tptz/PanTiltSpaces/VelocityGeneric",
                },
                "Zoom": {
                    "x": zoom,
                    "space": "http://www.onvif.org/ver10/tptz/ZoomSpaces/VelocityGeneric",
                },
            },
        }
        return self.client.ptz.ContinuousMove(params)

    def stop(self, profile_token, pan_tilt=True, zoom=True):
        return self.client.ptz.Stop(
            {"ProfileToken": profile_token, "PanTilt": pan_tilt, "Zoom": zoom}
        )

    def set_preset(self, profile_token, name, preset_token=None):
        params = {
            "ProfileToken": profile_token,
            "PresetName": name,
        }
        if preset_token:
            params["PresetToken"] = preset_token
        result = self.client.ptz.SetPreset(params)
        return str(result) if result is not None else ""

    def goto_preset(self, profile_token, preset_token, speed=1.0):
        params = {
            "ProfileToken": profile_token,
            "PresetToken": preset_token,
            "Speed": {
                "PanTilt": {"x": speed, "y": speed},
                "Zoom": {"x": speed},
            },
        }
        return self.client.ptz.GotoPreset(params)

    def remove_preset(self, profile_token, preset_token):
        return self.client.ptz.RemovePreset(
            {"ProfileToken": profile_token, "PresetToken": preset_token}
        )
