#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests",
#     "boto3",
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
import re
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

# --- Configuration ---
# 1. Paths
USER_HOME = Path.home()
RESTIC_BIN = "/usr/local/bin/restic"
PASS_FILE = USER_HOME / ".config/restic/password"
EXCLUDE_FILE = USER_HOME / ".config/restic/excludes.txt"
ENV_FILE = USER_HOME / ".config/restic/env"
LOCAL_LOG_FILE = "/tmp/restic_backup.log"

HOSTNAME = socket.gethostname()
LOGS_PREFIX = f"logs/{HOSTNAME}"

# --- Logging Setup ---
logging.basicConfig(
    filename=LOCAL_LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def load_shell_env(filepath):
    """
    Parses a bash-style env file (export VAR="VAL") and loads it into os.environ.
    Handles optional 'export' prefix and quoted values.
    """
    path = Path(filepath)
    if not path.exists():
        logging.error(f"Env file not found at: {filepath}")
        return

    # Regex to capture: (optional export) KEY="VALUE"
    # Group 1: Key
    # Group 2: Value (inside quotes) OR Group 3: Value (no quotes)
    pattern = re.compile(r'^(?:export\s+)?([a-zA-Z_][a-zA-Z0-9_]*)=(?:"([^"]*)"|\'([^\']*)\'|([^#\n\r]*))')

    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            match = pattern.match(line)
            if match:
                key = match.group(1)
                # Value matches one of the groups depending on quoting
                value = match.group(2) or match.group(3) or match.group(4) or ""
                os.environ[key] = value

def extract_bucket_name(repo_url):
    """
    Extracts the bucket name from a restic s3 repo string.
    Format examples:
      s3:s3.amazonaws.com/bucket-name
      s3:https://endpoint.com/bucket-name
      s3:bucket-name (if using AWS alias)
    """
    if not repo_url: return None
    
    # Remove 's3:' prefix
    clean_url = repo_url.replace("s3:", "", 1)
    
    # If it looks like a URL (contains /), parse it
    if "/" in clean_url:
        # If it starts with http, parse normally
        if clean_url.startswith("http"):
            path = urlparse(clean_url).path
            return path.strip("/").split("/")[0] # First segment is bucket
        else:
            # format: endpoint/bucket
            parts = clean_url.split("/", 1)
            # If the first part is a domain (has dots), assume second part is bucket/path
            if "." in parts[0] and len(parts) > 1:
                return parts[1].split("/")[0]
            return parts[0] # Fallback
            
    return clean_url

def check_power():
    """Returns True if on AC Power."""
    try:
        result = subprocess.run(["pmset", "-g", "ps"], capture_output=True, text=True)
        return "AC Power" in result.stdout
    except Exception as e:
        logging.error(f"Power check failed: {e}")
        return False

def upload_log_to_s3(content, status_tag, bucket_name):
    """Uploads the current run log to S3 under the hostname prefix."""
    if not bucket_name:
        logging.error("No bucket name found. Skipping log upload.")
        return

    try:
        # Boto3 will automatically pick up AWS_* keys from os.environ
        # Also handles AWS_ENDPOINT_URL if present in env file
        s3 = boto3.client('s3', endpoint_url=os.environ.get("AWS_ENDPOINT_URL")) 
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{LOGS_PREFIX}/{timestamp}_{status_tag}.txt"
        
        s3.put_object(
            Bucket=bucket_name,
            Key=filename,
            Body=content,
            ContentType="text/plain"
        )
        logging.info(f"Uploaded run log to s3://{bucket_name}/{filename}")
    except Exception as e:
        logging.error(f"Failed to upload log to S3: {e}")

def run_backup_logic():
    # Load Environment Variables first
    load_shell_env(ENV_FILE)
    
    repo_url = os.environ.get("RESTIC_REPOSITORY")
    kuma_url = os.environ.get("BACKUP_HEALTH_CHECK_URL")
    
    if not repo_url:
        logging.error("RESTIC_REPOSITORY not found in env file.")
        return

    bucket_name = extract_bucket_name(repo_url)

    start_time = time.time()
    run_log = []

    def log(msg, level="INFO"):
        """Logs to local file and S3 buffer."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        formatted = f"{timestamp} - {level} - {msg}"
        run_log.append(formatted)
        if level == "INFO": logging.info(msg)
        else: logging.error(msg)

    log(f"Starting backup for host: {HOSTNAME} on AC Power...")

    cmd = [
        RESTIC_BIN, "backup", str(USER_HOME),
        "--repo", repo_url,
        "--password-file", str(PASS_FILE),
        "--exclude-file", str(EXCLUDE_FILE),
        "--tag", "macos",
        "--host", HOSTNAME,
        "--json"
    ]

    # Current env includes the loaded variables
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
            if kuma_url:
                try:
                    # Append duration to the ping URL provided in ENV
                    full_url = f"{kuma_url}{duration_ms}"
                    requests.get(full_url, timeout=10)
                except Exception as e:
                    log(f"Kuma Ping Failed: {e}", "ERROR")
            else:
                log("No healthcheck URL configured.", "WARNING")

            upload_log_to_s3("\n".join(run_log), "SUCCESS", bucket_name)
        else:
            log(f"Backup Failed (Code {result.returncode})", "ERROR")
            upload_log_to_s3("\n".join(run_log), "FAILURE", bucket_name)

    except Exception as e:
        log(f"Critical Exception: {e}", "ERROR")
        upload_log_to_s3("\n".join(run_log), "CRITICAL", bucket_name)

def prevent_sleep_and_run():
    script_path = os.path.abspath(__file__)
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
