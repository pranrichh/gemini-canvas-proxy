# ☁️ Running on a VPS with Tailscale

Deploying to a VPS allows for a **24/7 "set it and forget it" private API**. Since Tailscale creates a secure mesh network, your proxy stays private (bound to `localhost`) but remains accessible from any of your devices on the tailnet.

## Prerequisites
- A Linux VPS (Ubuntu 22.04+ recommended, 2GB+ RAM)
- [Tailscale](https://tailscale.com/) account
- A Chromium-based browser (Chrome or Chromium)

## 1. Initial Setup
Install Tailscale and the browser dependencies on your VPS:
```bash
# Install Tailscale
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Install Chromium and Xvfb (for headless display)
sudo apt update
sudo apt install -y chromium-browser xvfb
```

## 2. Setup the Proxy
Clone the repo and run the setup script:
```bash
git clone https://github.com/pranrichh/gemini-canvas-proxy.git
cd gemini-canvas-proxy
chmod +x setup.sh
./setup.sh  # Follow prompts for Extension ID
```

## 3. Run Headless
Since Gemini Canvas needs a real browser tab, use `xvfb-run` to start Chromium in a virtual frame buffer:
```bash
# Start Chromium with a persistent profile
xvfb-run --server-args="-screen 0 1280x800x24" \
  chromium-browser --remote-debugging-port=9222 --user-data-dir=$HOME/.config/chromium-vps
```

## 4. Access via Tailscale
The proxy binds to `127.0.0.1:8765` on the VPS. To access it from your local machine:

**Option A: Global Binding (Private Tailnet Only)**
Modify `native_host/gemini_proxy.py` line 692:
```python
# Change from ('127.0.0.1', port) to ('0.0.0.0', port)
server = ThreadedHTTPServer(('0.0.0.0', port), APIHandler)
```
Now it's reachable at `http://<vps-tailscale-ip>:8765/v1` from any device on your tailnet.

**Option B: Tailscale Funnel (No Code Change)**
Keep it on `127.0.0.1` and use Tailscale Serve to expose it only to your tailnet:
```bash
tailscale serve 8765
```

---
**Tip:** Use a `systemd` unit or `screen`/`tmux` to keep the `xvfb-run` session alive 24/7.
