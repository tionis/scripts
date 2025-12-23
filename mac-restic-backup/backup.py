# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests",
#     "boto3",
#     "python-dotenv",
# ]
# ///

import subprocess
import sys
import time
import logging
import requests
import json
import os
import socket
import boto3
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# --- Configuration ---
# 1. Paths
USER_HOME = Path.home()
RESTIC_BIN = "/opt/homebrew/bin/restic"
PASS_FILE = USER_HOME / ".config/restic/password"
EXCLUDE_FILE = USER_HOME / ".config/restic/excludes.txt"
ENV_FILE = USER_HOME / ".config/restic/s3.env"
LOCAL_LOG_FILE = "/tmp/restic_backup.log"

# 2. Remote Settings
# Load Creds (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
load_dotenv(ENV_FILE)

REPO_URL = "s3:s3.amazonaws.com/your-bucket" # Restic Repository URL
BUCKET_NAME = "your-bucket"                  # Pure Bucket name for Boto3 logs
HOSTNAME = socket.gethostname()              # Dynamic Hostname (e.g., 'Moms-MacBook-Air')
LOGS_PREFIX = f"logs/{HOSTNAME}"             # S3 Path: logs/<hostname>/...

# 3. Monitoring
# Kuma URL (e.g. https://status.domain.com/api/push/KEY?status=up&msg=OK&ping=)
KUMA_BASE_URL = "https://status.yourdomain.com/api/push/YOUR_API_KEY"

# --- Setup ---
logging.basicConfig(
    filename=LOCAL_LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def check_power():
    """Returns True if on AC Power."""
    try:
        # pmset -g ps returns status. We look for 'AC Power'.
        result = subprocess.run(["pmset", "-g", "ps"], capture_output=True, text=True)
        return "AC Power" in result.stdout
    except Exception as e:
        logging.error(f"Power check failed: {e}")
        return False

def upload_log_to_s3(content, status_tag):
    """Uploads the current run log to S3 under the hostname prefix."""
    try:
        s3 = boto3.client('s3')
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{LOGS_PREFIX}/{timestamp}_{status_tag}.txt"
        
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=filename,
            Body=content,
            ContentType="text/plain"
        )
        logging.info(f"Uploaded run log to s3://{BUCKET_NAME}/{filename}")
    except Exception as e:
        logging.error(f"Failed to upload log to S3: {e}")

def run_backup_logic():
    start_time = time.time()
    run_log = [] # buffer for S3 upload

    def log(msg, level="INFO"):
        """Logs to local file and S3 buffer."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"{timestamp} - {level} - {msg}"
        run_log.append(formatted)
        if level == "INFO": logging.info(msg)
        else: logging.error(msg)

    log(f"Starting backup for host: {HOSTNAME} on AC Power...")

    # Restic Command
    cmd = [
        RESTIC_BIN, "backup", str(USER_HOME),
        "--repo", REPO_URL,
        "--password-file", str(PASS_FILE),
        "--exclude-file", str(EXCLUDE_FILE),
        "--tag", "macos",
        "--host", HOSTNAME, # Force Restic to use the same hostname
        "--json"
    ]

    # Pass environment (with loaded AWS keys) to subprocess
    env = os.environ.copy()

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        duration_ms = int((time.time() - start_time) * 1000)

        # Capture Output
        run_log.append("\n--- Restic STDOUT ---")
        run_log.append(result.stdout)
        run_log.append("\n--- Restic STDERR ---")
        run_log.append(result.stderr)

        if result.returncode == 0:
            log(f"Backup Successful. Duration: {duration_ms}ms")
            
            # Ping Kuma
            try:
                url = f"{KUMA_BASE_URL}?status=up&msg=OK&ping={duration_ms}"
                requests.get(url, timeout=10)
            except Exception as e:
                log(f"Kuma Ping Failed: {e}", "ERROR")

            upload_log_to_s3("\n".join(run_log), "SUCCESS")
        else:
            log(f"Backup Failed (Code {result.returncode})", "ERROR")
            upload_log_to_s3("\n".join(run_log), "FAILURE")

    except Exception as e:
        log(f"Critical Exception: {e}", "ERROR")
        upload_log_to_s3("\n".join(run_log), "CRITICAL")

def prevent_sleep_and_run():
    script_path = os.path.abspath(__file__)
    # Recursive call inside caffeinate using uv
    cmd = ["/usr/bin/caffeinate", "-ism", "uv", "run", script_path, "--run-logic"]
    subprocess.run(cmd)

if __name__ == "__main__":
    if "--run-logic" in sys.argv:
        run_backup_logic()
    else:
        if check_power():
            prevent_sleep_and_run()
        else:
            logging.info("On Battery. Skipping.")
