# Mac Restic Backup

A robust, "deploy-and-forget" backup solution designed for macOS laptops. It uses **Restic** for efficient, encrypted, deduplicated backups to S3, wrapped in a **Python** script that manages power states and monitoring.

## Features

* **Power Aware:** Only runs when connected to AC power to preserve battery.
* **Sleep Prevention:** Uses `caffeinate` to prevent system and disk sleep during backup execution.
* **Self-Contained:** Uses `uv` and PEP 723 inline dependencies to manage Python environments automatically.
* **Monitoring:** Pings an **Uptime Kuma** instance with execution duration (ms) upon success.
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

* **Script:** `~/.local/bin/backup_macos.py`
* **LaunchAgent:** `~/Library/LaunchAgents/dev.tionis.scripts.mac-restic-backup.plist`

*Ensure the script is executable (optional with `uv` but good practice):*
```bash
chmod +x ~/.local/bin/backup_macos.py

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

**C. Environment & Secrets**
File: `~/.config/restic/s3.env`
*Secure this file: `chmod 600 ~/.config/restic/s3.env*`

```ini
AWS_ACCESS_KEY_ID=your_key_id
AWS_SECRET_ACCESS_KEY=your_secret_key

```

### 3. Customize Script Variables

Edit the top configuration section of `backup_macos.py` to match your infrastructure:

* `REPO_URL`: Your S3 bucket address (e.g., `s3:s3.amazonaws.com/my-backups`).
* `BUCKET_NAME`: The raw bucket name for log uploading (e.g., `my-backups`).
* `KUMA_BASE_URL`: Your Uptime Kuma push URL.

### 4. Full Disk Access (Critical)

MacOS requires explicit permission to access user files (Documents, Photos, Mail) and prevent sleep.

1. Open **System Settings** -> **Privacy & Security** -> **Full Disk Access**.
2. Grant access to:
* **Terminal** (for the initial run).
* **Restic binary** (`/opt/homebrew/bin/restic`).
* **Python/uv** (If execution fails, add `~/.cargo/bin/uv`).



### 5. Activate Schedule

Load the LaunchAgent to run hourly.

```bash
launchctl load ~/Library/LaunchAgents/dev.tionis.scripts.mac-restic-backup.plist

```

## Usage & Maintenance

### Initial Run

The first backup should be run manually to seed the repository, as it may take longer than a typical "lid-open" session.

```bash
# Run with logic flag to trigger the backup immediately
uv run ~/.local/bin/backup_macos.py --run-logic

```

### Verification

* **Local Logs:** `tail -f /tmp/restic_backup.log`
* **Remote Logs:** Check S3 path `s3://<bucket>/logs/<hostname>/`
* **Job Status:** `launchctl list | grep dev.tionis`

### Remote Troubleshooting

If the Uptime Kuma ping is missing:

1. Check the **S3 Logs** folder first for error messages (Lock exists, Network timeout, etc.).
2. SSH into the machine.
3. Check if the process is stuck: `ps aux | grep restic`.
4. Unlock repo if needed: `restic unlock --repo ...`

## License

MIT
