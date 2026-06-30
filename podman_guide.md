# Podman Cheat Sheet: Gemini Canvas Proxy

This guide contains the most common commands you will need to manage, toggle, and debug the `gemini-canvas-proxy` container running on your Ubuntu server (PC2).

---

## 🔋 Headless Mode (VNC Toggles)

To toggle the VNC desktop server (which runs the browser canvas) on or off to save CPU/RAM:

### 1. Stop VNC (Go Headless — Saves Resources)
```bash
podman exec -it gemini-canvas-proxy /app/toggle-vnc.sh stop
```
*Stops `x11vnc` and `websockify`. The browser continues to run in the background, keeping the proxy alive.*

### 2. Start VNC (Reactivate UI for Login/Inspection)
```bash
podman exec -it gemini-canvas-proxy /app/toggle-vnc.sh start
```
*Re-spawns the desktop and WebSocket interface so you can access the noVNC UI again.*

---

## 🛠️ Diagnostics & Debugging

Run these commands to inspect the state of the container if requests are hanging or failing:

### 1. View Running Processes inside Container
```bash
podman exec -it gemini-canvas-proxy ps aux
```

### 2. View Chromium Console/Error Logs
```bash
podman exec -it gemini-canvas-proxy cat /tmp/chromium.log
```

### 3. View Python Proxy Logs (Chat completions history)
```bash
podman exec -it gemini-canvas-proxy cat /tmp/proxy.log
```

### 4. Enter a Shell inside the Container
```bash
podman exec -it gemini-canvas-proxy /bin/bash
```

---

## ⚙️ Extension ID Registration

If you ever need to load a new unpacked extension (or if the ID changes on a fresh profile):

```bash
podman exec -it gemini-canvas-proxy /app/setup-extension.sh <32-character-extension-id>
```
*Example:*
```bash
podman exec -it gemini-canvas-proxy /app/setup-extension.sh edoicfpldmlabgdalemfgflpldiijdmm
```
*(After updating, run `systemctl --user restart gemini-canvas-proxy.service` to apply changes).*

---

## 📦 Host Service Control

Run these on the PC2 server command-line (outside the container) under the `shadowplague` user:

* **Restart Service:** `systemctl --user restart gemini-canvas-proxy.service`
* **Stop Service:** `systemctl --user stop gemini-canvas-proxy.service`
* **Start Service:** `systemctl --user start gemini-canvas-proxy.service`
* **Check Service Status:** `systemctl --user status gemini-canvas-proxy.service`
* **View Systemd Logs (tail):** `journalctl --user -n 50 -u gemini-canvas-proxy.service`
* **View Systemd Logs (follow):** `journalctl --user -f -u gemini-canvas-proxy.service`
