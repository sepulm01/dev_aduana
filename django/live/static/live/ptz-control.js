class PTZControl {
  constructor(config) {
    this.overlayEl = config.overlayEl;
    this.containerEl = config.containerEl;
    this.toggleBtnEl = config.toggleBtnEl;
    this.statusEl = config.statusEl;
    this.moveUrl = config.moveUrl;
    this.statusUrl = config.statusUrl;
    this.profileToken = config.profileToken || "";
    this.currentProfileToken = config.profileToken || "";
    this.suffix = config.suffix || "";

    const specs = config.cameraSpecs || {};
    this.hFovWide = specs.h_fov_wide ?? 99.1;
    this.hFovTele = specs.h_fov_tele ?? 31.9;
    this.vFovWide = specs.v_fov_wide ?? 53.4;
    this.vFovTele = specs.v_fov_tele ?? 18.0;
    this.panRange = specs.pan_range ?? 355;
    this.tiltRange = specs.tilt_range ?? 90;

    this.ptzPan = 0.0;
    this.ptzTilt = 0.0;
    this.ptzZoom = 0.0;
    this.ptzSupported = config.ptzSupported === true;
    this.ptzModeEnabled = false;
    this.zoomTimeout = null;
    this.lastManualMove = false;

    this._boundClick = this._onClick.bind(this);
    this._boundWheel = this._onWheel.bind(this);
    this._boundKeydown = this._onKeydown.bind(this);
    this._boundFullscreenChange = this._onFullscreenChange.bind(this);
  }

  init() {
    if (!this.ptzSupported) return;
    if (this.currentProfileToken) {
      this.checkStatus();
    }
    this.overlayEl.addEventListener("click", this._boundClick);
    this.overlayEl.addEventListener("wheel", this._boundWheel, { passive: false });
    document.addEventListener("keydown", this._boundKeydown);
    document.addEventListener("fullscreenchange", this._boundFullscreenChange);
  }

  destroy() {
    this.overlayEl.removeEventListener("click", this._boundClick);
    this.overlayEl.removeEventListener("wheel", this._boundWheel);
    document.removeEventListener("keydown", this._boundKeydown);
    document.removeEventListener("fullscreenchange", this._boundFullscreenChange);
  }

  setProfileToken(token) {
    this.profileToken = token;
    this.currentProfileToken = token;
    if (token) {
      this.checkStatus();
    }
  }

  togglePtzMode() {
    if (!this.ptzSupported) return;
    this.ptzModeEnabled = !this.ptzModeEnabled;
    this._showOverlay(this.ptzModeEnabled);
    this.toggleBtnEl.classList.toggle("btn-outline-warning", !this.ptzModeEnabled);
    this.toggleBtnEl.classList.toggle("btn-warning", this.ptzModeEnabled);
  }

  _showOverlay(show) {
    this.overlayEl.style.display = show ? "block" : "none";
  }

  async checkStatus() {
    if (!this.currentProfileToken) return;
    try {
      const resp = await fetch(`${this.statusUrl}?profile_token=${this.currentProfileToken}`);
      const data = await resp.json();
      this.ptzSupported = data.ptz_supported;
      if (this.ptzSupported) {
        this.toggleBtnEl.classList.remove("d-none");
        if (this.statusEl) {
          this.statusEl.textContent = "Disponible";
          this.statusEl.className = "text-success";
        }
        if (data.status) {
          this.ptzPan = data.status.pan || 0;
          this.ptzTilt = data.status.tilt || 0;
          this.ptzZoom = data.status.zoom || 0;
        }
      } else if (this.statusEl) {
        this.statusEl.textContent = "No soportado";
        this.statusEl.className = "text-muted";
      }
    } catch (e) {
      if (this.statusEl) {
        this.statusEl.textContent = "Error";
        this.statusEl.className = "text-danger";
      }
    }
  }

  _onClick(e) {
    if (!this.ptzSupported || !this.currentProfileToken) return;
    const rect = this.overlayEl.getBoundingClientRect();
    const clickX = (e.clientX - rect.left) / rect.width;
    const clickY = (e.clientY - rect.top) / rect.height;

    const hFov = this.hFovWide * (1 - this.ptzZoom) + this.hFovTele * this.ptzZoom;
    const vFov = this.vFovWide * (1 - this.ptzZoom) + this.vFovTele * this.ptzZoom;

    const offsetPan = (clickX - 0.5) * hFov / (this.panRange / 2);
    const offsetTilt = (0.5 - clickY) * vFov / (this.tiltRange / 2);

    const newPan = Math.max(-1, Math.min(1, this.ptzPan + offsetPan));
    const newTilt = Math.max(-1, Math.min(1, this.ptzTilt + offsetTilt));

    this.ptzPan = newPan;
    this.ptzTilt = newTilt;

    fetch(this.moveUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        profile_token: this.currentProfileToken,
        type: "absolute",
        pan: newPan,
        tilt: newTilt,
        zoom: this.ptzZoom,
      }),
    }).catch(e => console.error("PTZ error:", e));

    this.lastManualMove = true;
    document.dispatchEvent(new CustomEvent("ptz-manual-move", {
      detail: { pan: newPan, tilt: newTilt, zoom: this.ptzZoom },
    }));
  }

  _onWheel(e) {
    if (!this.ptzSupported || !this.currentProfileToken) return;
    e.preventDefault();

    this.ptzZoom = Math.max(0, Math.min(1, this.ptzZoom + (e.deltaY > 0 ? -0.05 : 0.05)));

    if (this.zoomTimeout) clearTimeout(this.zoomTimeout);
    this.zoomTimeout = setTimeout(() => {
      fetch(this.moveUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          profile_token: this.currentProfileToken,
          type: "absolute",
          pan: this.ptzPan,
          tilt: this.ptzTilt,
          zoom: this.ptzZoom,
        }),
      }).catch(e => console.error("PTZ zoom error:", e));

      this.lastManualMove = true;
      document.dispatchEvent(new CustomEvent("ptz-manual-move", {
        detail: { pan: this.ptzPan, tilt: this.ptzTilt, zoom: this.ptzZoom },
      }));
    }, 150);
  }

  _onKeydown(e) {
    if (e.key === "Control" && !e.repeat && this.currentProfileToken && this.ptzSupported) {
      e.preventDefault();
      this.togglePtzMode();
    }
  }

  _onFullscreenChange() {
    const icon = document.querySelector("#fullscreenBtn" + this.suffix + " i");
    if (!icon) return;
    icon.className = document.fullscreenElement
      ? "bi bi-fullscreen-exit"
      : "bi bi-arrows-fullscreen";
  }
}

function initVideoPlayer(suffix) {
  const sfx = suffix || "";
  const frame = document.getElementById("videoFrame" + sfx);
  const container = document.getElementById("videoContainer" + sfx);
  const noVideo = document.getElementById("noVideo" + sfx);
  const status = document.getElementById("streamStatus" + sfx);
  if (!frame) return;

  function onLoaded() {
    if (noVideo) noVideo.style.display = "none";
    if (container) container.style.display = "block";
  }

  frame.addEventListener("load", onLoaded);

  if (frame.src) {
    onLoaded();
  }
}

function toggleFullscreen(suffix) {
  const sfx = suffix || "";
  const container = document.getElementById("videoContainer" + sfx);
  if (!container) return;
  if (!document.fullscreenElement) {
    container.requestFullscreen();
  } else {
    document.exitFullscreen();
  }
}
