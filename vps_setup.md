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

## 3. Web-Based Browser Access (noVNC)
This is the simplest way to interact with the VPS browser. It streams the browser directly to your local laptop's browser over Tailscale.

1. Install the streaming tools on the VPS:
   ```bash
   sudo apt update
   sudo apt install -y x11vnc xvfb novnc websockify
   ```

2. Start the browser stream:
   ```bash
   # 1. Start virtual display
   Xvfb :99 -screen 0 1280x720x16 &
   export DISPLAY=:99
   
   # 2. Launch Chromium inside the virtual display
   chromium-browser --user-data-dir=$HOME/.config/chromium-vps --no-first-run &
   
   # 3. Stream that display to a web port (6080)
   x11vnc -display :99 -nopw -forever -xkb &
   websockify --web /usr/share/novnc/ 6080 localhost:5900 &
   ```

3. **In your local browser**, navigate to:
   `http://<vps-tailscale-ip>:6080/vnc.html?autoconnect=true`

4. **In the Web Window:**
   - You will see the VPS Chromium browser.
   - Log in to [gemini.google.com](https://gemini.google.com).
   - Go to `chrome://extensions`, enable **Developer Mode**, and **Load Unpacked** the `extension/` folder.
   - **Copy the Extension ID** and start a Canvas session.

## 4. Final Proxy Setup
Once you have the Extension ID, run the setup script on the VPS:
```bash
./setup.sh  # Paste the ID when prompted
```

## 5. Headless Production Mode
After you've logged in and loaded the extension, the browser profile is saved. You can now kill the VNC/noVNC processes and run Chromium 24/7 headlessly:

```bash
# Start Chromium headlessly (saved in your tmux/screen session)
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
