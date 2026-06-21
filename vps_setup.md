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

## 3. Interactive Setup (Tailscale + Browser)
Instead of complex VNC setups, we use Chromium's built-in **Remote Debugging**. This lets you "see" and interact with the VPS browser directly from your local laptop's browser.

1. Start Chromium on the VPS with remote debugging enabled:
   ```bash
   xvfb-run --server-args="-screen 0 1280x800x24" \
     chromium-browser --remote-debugging-port=9222 \
     --remote-debugging-address=0.0.0.0 \
     --user-data-dir=$HOME/.config/chromium-vps
   ```

2. On your **local laptop**, open your browser and navigate to:
   `http://<vps-tailscale-ip>:9222`

3. **In your local browser:**
   - Click the **"Screencast" icon** (top-left, looks like a tiny monitor).
   - **You can now see and control the VPS browser!**
   - Navigate to [gemini.google.com](https://gemini.google.com) and log in.
   - Go to `chrome://extensions`, enable **Developer Mode**, and **Load Unpacked** the `extension/` folder.
   - **Copy the Extension ID** and start a Canvas session.

## 4. Final Proxy Setup
Once you have the Extension ID, run the setup script on the VPS:
```bash
./setup.sh  # Paste the ID when prompted
```

## 5. Headless Production Mode
After the initial setup is done, you can stop the previous command and run Chromium without the debugging port for better performance:
```bash
xvfb-run --server-args="-screen 0 1280x800x24" \
  chromium-browser --user-data-dir=$HOME/.config/chromium-vps
```

## 6. Access via Tailscale
The proxy now defaults to binding to `0.0.0.0:8765`, meaning it is reachable from any interface.

**Private Access (Recommended)**
If you are running Tailscale on the VPS, the proxy is automatically reachable at:
`http://<vps-tailscale-ip>:8765/v1`

**Tailscale Funnel (Alternative)**
If you want to keep the proxy bound specifically to a single interface or use Tailscale's built-in relaying:
```bash
tailscale serve 8765
```

---
**Tip:** Use a `systemd` unit or `screen`/`tmux` to keep the `xvfb-run` session alive 24/7.
