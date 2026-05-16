class PTZService:
    """Wraps the ONVIF PTZ SOAP service for movement, presets, and status."""

    def __init__(self, client):
        self.client = client

    def get_status(self, profile_token):
        """Return current PTZ position (pan/tilt/zoom) or None on failure."""
        try:
            return self.client.ptz.GetStatus({"ProfileToken": profile_token})
        except Exception:
            return None

    def get_presets(self, profile_token):
        """Return list of saved PTZ presets for a profile."""
        try:
            return self.client.ptz.GetPresets({"ProfileToken": profile_token})
        except Exception:
            return []

    def get_configuration(self, profile_token):
        """Return the PTZ configuration for a profile."""
        try:
            return self.client.ptz.GetConfiguration({"ProfileToken": profile_token})
        except Exception:
            return None

    def absolute_move(self, profile_token, pan=0.0, tilt=0.0, zoom=0.0):
        """Move PTZ to an absolute position (-1 to 1 range)."""
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
        """Start continuous PTZ movement at a given velocity (-1 to 1)."""
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
        """Stop PTZ movement (pan/tilt and/or zoom)."""
        return self.client.ptz.Stop(
            {"ProfileToken": profile_token, "PanTilt": pan_tilt, "Zoom": zoom}
        )

    def set_preset(self, profile_token, name, preset_token=None):
        """Save current position as a named preset.

        If preset_token is provided, overwrite that existing preset.
        """
        params = {
            "ProfileToken": profile_token,
            "PresetName": name,
        }
        if preset_token:
            params["PresetToken"] = preset_token
        result = self.client.ptz.SetPreset(params)
        return str(result) if result is not None else ""

    def goto_preset(self, profile_token, preset_token, speed=1.0):
        """Move PTZ to a saved preset at a given speed multiplier."""
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
        """Delete a saved preset by its token."""
        return self.client.ptz.RemovePreset(
            {"ProfileToken": profile_token, "PresetToken": preset_token}
        )
