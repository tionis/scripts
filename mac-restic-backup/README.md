# Mac Restic Backup

A robust, "deploy-and-forget" backup solution designed for macOS laptops. It uses **Restic** for efficient, encrypted, deduplicated backups to S3, wrapped in a **Python** script that manages power states and monitoring.

## Features

* **Power Aware:** Only runs when connected to AC power to preserve battery.
* **Sleep Prevention:** Uses `caffeinate` to prevent system and disk sleep during backup execution.
* **Self-Contained:** Uses `uv` and PEP 723 inline dependencies to manage Python environments automatically.
* **Monitoring:** Pings a health check URL (e.g., Uptime Kuma) with execution duration (ms) upon success.
* **Remote Logging:** Uploads run logs (STDOUT/STDERR) to the S3 bucket (`logs/<hostname>/`) for remote troubleshooting.
* **Invisible:** Runs silently in the background via `launchd`.

## Prerequisites

The target macOS machine requires:
1.  **Homebrew**
2.  **Restic**: `brew install restic`
3.  **uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Installation

### 1. Place Files
Deploy the script and plist to the user's local directories.

* **Script:** `~/.local/bin/backup.py`
* **LaunchAgent:** `~/Library/LaunchAgents/dev.tionis.scripts.mac-restic-backup.plist`

*Ensure the script is executable:*
```bash
chmod +x ~/.local/bin/backup.py

```

### 2. Configuration

Create the necessary configuration files in `~/.config/restic/`.

**A. Repository Password**
File: `~/.config/restic/password`
*Content: Plain text password.*

**B. Excludes**
File: `~/.config/restic/excludes.txt`
*Standard macOS exclusions:*

```text
.DS_Store
.Trash
Library/Caches
Library/Logs
Downloads
node_modules
__pycache__

```

**C. Environment Variables (Secrets & Config)**
File: `~/.config/restic/env`
*Content: Standard Bash export format. This file is parsed by the script.*

```bash
# Restic Repository Location
export RESTIC_REPOSITORY="s3://s3.amazonaws.com/your-bucket/macos-repo"

# AWS / S3 Credentials
export AWS_ACCESS_KEY_ID="your_key_id"
export AWS_SECRET_ACCESS_KEY="your_secret_key"
# Optional: Endpoint for non-AWS S3
# export AWS_ENDPOINT_URL="[https://s3.us-west-1.wasabisys.com](https://s3.us-west-1.wasabisys.com)"

# Healthcheck URL (Duration in ms will be appended to the end)
export BACKUP_HEALTH_CHECK_URL="https://status.yourdomain.com/api/push/KEY?status=up&msg=OK&ping="

```

*Secure this file:*

```bash
chmod 600 ~/.config/restic/env

```

### 3. Full Disk Access (Critical)

MacOS requires explicit permission to access user files (Documents, Photos, Mail) and prevent sleep.

1. Open **System Settings** -> **Privacy & Security** -> **Full Disk Access**.
2. Grant access to:
* **Restic binary** (`/opt/homebrew/bin/restic`).
* **uv** (`~/.local/bin/uv` or `~/.cargo/bin/uv` - highly recommended).



### 4. Activate Schedule

Load the LaunchAgent to run hourly.

```bash
launchctl load ~/Library/LaunchAgents/dev.tionis.scripts.mac-restic-backup.plist

```

## Usage & Maintenance

### Initial Run

The first backup should be run manually to seed the repository.

```bash
# Run with logic flag to trigger the backup immediately
uv run ~/.local/bin/backup.py --run-logic

```

### Verification

* **Local Logs:** `tail -f /tmp/restic_backup.log`
* **Remote Logs:** Check S3 path `s3://<bucket>/logs/<hostname>/`
* **Job Status:** `launchctl list | grep dev.tionis`

### Remote Troubleshooting

1. Check **S3 Logs** for error messages (Lock exists, Network timeout).
2. SSH into the machine.
3. Check if process is stuck: `ps aux | grep restic`.
4. Unlock repo if needed: `restic unlock` (env vars are automatically loaded if you source the env file: `source ~/.config/restic/env`).

## License

MIT