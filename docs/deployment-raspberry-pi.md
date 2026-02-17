# Raspberry Pi 4 Deployment (Raspberry Pi OS 64-bit)

This guide deploys the bot as a `systemd` service using `uv` as the only supported launcher.
All runtime files stay under `/opt/copilot-telegram`, except the service unit installed at `/etc/systemd/system/copilot-telegram.service`.

## Target

- Hardware: Raspberry Pi 4
- OS: Raspberry Pi OS 64-bit
- Service manager: `systemd`
- Runtime launcher: `uv` (required)

## Prerequisites

1. A Raspberry Pi OS 64-bit installation with network access.
2. A Telegram bot token available for `TELEGRAM_BOT_API_KEY`.
3. `git`, `curl`, and `sudo` installed.

## Install `uv`

Install `uv` and verify it is on `PATH`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
```

If `uv --version` fails, add `~/.local/bin` to your shell `PATH` and retry.

## Install and configure the bot

From the Raspberry Pi shell:

```bash
sudo mkdir -p /opt
sudo chown "$USER":"$USER" /opt
git clone <your-repo-url> /opt/copilot-telegram
cd /opt/copilot-telegram
uv pip install -r requirements.txt
```

Install the service files:

```bash
UV_BIN="$(command -v uv)"
COPILOT_BIN="$(command -v copilot)"
sudo ./scripts/install_rpi_service.sh --repo-dir /opt/copilot-telegram --uv-bin "$UV_BIN" --copilot-bin "$COPILOT_BIN"
```

To run the service as a different Linux user, pass `--service-user <user>` or set `COPILOT_SERVICE_USER` before invoking the installer.

The installer creates the runtime environment file at:

```bash
/opt/copilot-telegram/runtime/copilot-telegram.env
```

If `deploy/raspberry-pi/copilot-telegram.env.example` is missing in your checkout, the installer will still create this file using built-in defaults.

Edit `/opt/copilot-telegram/runtime/copilot-telegram.env` and set at minimum:

```bash
TELEGRAM_BOT_API_KEY=<your-bot-api-key>
GITHUB_TOKEN=<github-token-for-copilot>
```

`GH_TOKEN` is also supported; when both are present, `GITHUB_TOKEN` is used.

## Enable and start the service

```bash
sudo systemctl enable --now copilot-telegram.service
sudo systemctl status copilot-telegram.service --no-pager
```

Expected state:

- `Active: active (running)`
- `ExecStart` uses `uv run chat-telegram.py --working-directory ...`

## Validate after reboot

```bash
sudo reboot
# reconnect
sudo systemctl status copilot-telegram.service --no-pager
sudo systemctl is-enabled copilot-telegram.service
```

Expected state:

- Service remains `active (running)`.
- `systemctl is-enabled` prints `enabled`.

## Logs and troubleshooting

View recent logs:

```bash
sudo journalctl -u copilot-telegram.service -n 100 --no-pager
```

Common issues:

- `uv: command not found`: install `uv` and ensure it is on `PATH` before running installer.
- `uv` is installed but installer cannot find it under `sudo`: pass `--uv-bin "$(command -v uv)"`.
- `FileNotFoundError: ... 'copilot'`: pass `--copilot-bin "$(command -v copilot)"` (or set `COPILOT_CLI_PATH`) and rerun installer so runtime env gets an absolute `COPILOT_CLI_PATH`.
- Missing token: set `TELEGRAM_BOT_API_KEY` in `/opt/copilot-telegram/runtime/copilot-telegram.env`.
- Startup failure after changes: run `sudo systemctl daemon-reload && sudo systemctl restart copilot-telegram.service`.

## Upgrade workflow

```bash
cd /opt/copilot-telegram
git pull
uv pip install -r requirements.txt
UV_BIN="$(command -v uv)"
COPILOT_BIN="$(command -v copilot)"
sudo ./scripts/install_rpi_service.sh --repo-dir /opt/copilot-telegram --uv-bin "$UV_BIN" --copilot-bin "$COPILOT_BIN"
sudo systemctl restart copilot-telegram.service
```

## Uninstall

```bash
sudo systemctl disable --now copilot-telegram.service
sudo rm -f /etc/systemd/system/copilot-telegram.service
sudo systemctl daemon-reload
sudo rm -f /opt/copilot-telegram/runtime/copilot-telegram.env
```
