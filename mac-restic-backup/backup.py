>#!/usr/bin/env -S uv run --script
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
# Log to file by default
logging.basicConfig(
    filename=LOCAL_LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# If running interactively, also log to stdout
if sys.stdout.isatty():
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(message)s')
    console.setFormatter(formatter)
    logging.getLogger('').addHandler(console)

def load_shell_env(filepath):
    """
    Parses a bash-style env file (export VAR="VAL") and loads it into os.environ.
    """
    path = Path(filepath)
    if not path.exists():
        logging.error(f"Env file not found at: {filepath}")
        return

    pattern = re.compile(r'^(?:export\s+)?([a-zA-Z_][a-zA-Z0-9_]*)=(?:"([^"]*)"|\'([^\']*)\'|([^#\n\r]*))')

    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            match = pattern.match(line)
            if match:
                key = match.group(1)
                value = match.group(2) or match.group(3) or match.group(4) or ""
                os.environ[key] = value

def parse_repo_config(repo_url):
    """
    Parses Restic S3 URL to extract bucket name and endpoint URL for Boto3.
    Returns: (bucket_name, endpoint_url)
    
    Handles:
      s3:https://host.com/bucket      -> (bucket, https://host.com)
      s3:host.com/bucket              -> (bucket, https://host.com)
      s3:s3.amazonaws.com/bucket      -> (bucket, None) [AWS Default]
      s3:bucket                       -> (bucket, None) [AWS Default]
    """
    if not repo_url: return None, None
    
    clean_url = repo_url.replace("s3:", "", 1)
    
    # Case 1: Explicit Scheme (s3:https://endpoint/bucket)
    if clean_url.startswith("http"):
        parsed = urlparse(clean_url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}"
        path_parts = parsed.path.strip("/").split("/")
        bucket = path_parts[0] if path_parts else None
        return bucket, endpoint

    # Case 2: AWS Alias (s3:bucketname) - No slashes usually
    if "/" not in clean_url:
        return clean_url, None

    # Case 3: Host/Bucket (s3:endpoint.com/bucket)
    parts = clean_url.split("/", 1)
    host = parts[0]
    bucket_path = parts[1] if len(parts) > 1 else ""
    bucket = bucket_path.split("/")[0]

    # Check if it's standard AWS
    if "amazonaws.com" in host:
        return bucket, None
    
    # Assume HTTPS for custom endpoints if scheme is missing
    return bucket, f"https://{host}"

def check_power():
    """Returns True if on AC Power."""
    try:
        result = subprocess.run(["pmset", "-g", "ps"], capture_output=True, text=True)
        return "AC Power" in result.stdout
    except Exception as e:
        logging.error(f"Power check failed: {e}")
        return False

def upload_log_to_s3(content, status_tag, bucket_name, endpoint_url):
    """Uploads the current run log to S3."""
    if not bucket_name:
        logging.error("No bucket name found. Skipping log upload.")
        return

    try:
        # Boto3 uses endpoint_url if provided, otherwise defaults to AWS
        s3 = boto3.client('s3', endpoint_url=endpoint_url)
        
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
    load_shell_env(ENV_FILE)
    
    repo_url = os.environ.get("RESTIC_REPOSITORY")
    kuma_url = os.environ.get("BACKUP_HEALTH_CHECK_URL")
    
    if not repo_url:
        logging.error("RESTIC_REPOSITORY not found in env file.")
        return

    # Extract bucket and endpoint from the Restic URL
    bucket_name, endpoint_url = parse_repo_config(repo_url)
    
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
    if endpoint_url:
        log(f"Detected Custom Endpoint: {endpoint_url}")

    cmd = [
        RESTIC_BIN, "backup", str(USER_HOME),
        "--repo", repo_url,
        "--password-file", str(PASS_FILE),
        "--exclude-file", str(EXCLUDE_FILE),
        "--tag", "macos",
        "--host", HOSTNAME,
        "--json"
    ]

    env = os.environ.copy()

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        duration_ms = int((time.time() - start_time) * 1000)

        run_log.append("\n--- Restic STDOUT ---")
        run_log.append(result.stdout)
        run_log.append("\n--- Restic STDERR ---")
        run_log.append(result.stderr)

        if result.returncode == 0:
            log(f"Backup Successful. Duration: {duration_ms}ms")
            
            if kuma_url:
                try:
                    full_url = f"{kuma_url}{duration_ms}"
                    requests.get(full_url, timeout=10)
                except Exception as e:
                    log(f"Kuma Ping Failed: {e}", "ERROR")

            upload_log_to_s3("\n".join(run_log), "SUCCESS", bucket_name, endpoint_url)
        else:
            log(f"Backup Failed (Code {result.returncode})", "ERROR")
            upload_log_to_s3("\n".join(run_log), "FAILURE", bucket_name, endpoint_url)

    except Exception as e:
        log(f"Critical Exception: {e}", "ERROR")
        upload_log_to_s3("\n".join(run_log), "CRITICAL", bucket_name, endpoint_url)

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
