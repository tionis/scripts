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
    path = Path(filepath)
    if not path.exists():
        logging.error(f"Env file not found at: {filepath}")
        return

    pattern = re.compile(r'^(?:export\s+)?([a-zA-Z_][a-zA-Z0-9_]*)=(?:"([^"]*)"|\'([^\']*)\'|([^#\n\r]*))')

    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'): continue
            match = pattern.match(line)
            if match:
                key = match.group(1)
                value = match.group(2) or match.group(3) or match.group(4) or ""
                os.environ[key] = value

def parse_repo_config(repo_url):
    if not repo_url: return None, None
    clean_url = repo_url.replace("s3:", "", 1)
    
    if clean_url.startswith("http"):
        parsed = urlparse(clean_url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}"
        path_parts = parsed.path.strip("/").split("/")
        bucket = path_parts[0] if path_parts else None
        return bucket, endpoint

    if "/" not in clean_url: return clean_url, None

    parts = clean_url.split("/", 1)
    host, bucket_path = parts[0], parts[1] if len(parts) > 1 else ""
    bucket = bucket_path.split("/")[0]
    
    if "amazonaws.com" in host: return bucket, None
    return bucket, f"https://{host}"

def check_power():
    try:
        result = subprocess.run(["pmset", "-g", "ps"], capture_output=True, text=True)
        return "AC Power" in result.stdout
    except Exception:
        return False

def upload_log_to_s3(content, status_tag, bucket_name, endpoint_url):
    if not bucket_name: return
    try:
        s3 = boto3.client('s3', endpoint_url=endpoint_url)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"{LOGS_PREFIX}/{timestamp}_{status_tag}.txt"
        s3.put_object(Bucket=bucket_name, Key=filename, Body=content, ContentType="text/plain")
        logging.info(f"Uploaded log to s3://{bucket_name}/{filename}")
    except Exception as e:
        logging.error(f"S3 Upload Failed: {e}")

# --- NEW: Live Progress Monitoring ---
def monitor_process(cmd, env):
    """
    Runs the command, streams output live to console (if interactive),
    and filters logs for S3 (removing noisy progress updates).
    """
    process = subprocess.Popen(
        cmd, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.STDOUT, 
        text=True, 
        env=env,
        bufsize=1 # Line buffered
    )

    captured_logs = []
    is_interactive = sys.stdout.isatty()

    # Iterate over stdout line by line
    for line in process.stdout:
        line = line.strip()
        if not line: continue

        try:
            data = json.loads(line)
            msg_type = data.get("message_type")

            # 1. Handle Progress Updates (type: status)
            if msg_type == "status":
                # Only show in terminal, DO NOT save to log buffer
                if is_interactive:
                    percent = data.get("percent_done", 0.0) * 100
                    seconds = data.get("seconds_remaining", 0)
                    files = data.get("total_files", 0)
                    # Overwrite the current line with \r
                    sys.stdout.write(f"\rProgress: [{percent:5.1f}%] {files} files... ETA: {seconds}s")
                    sys.stdout.flush()
            
            # 2. Handle Summary/Errors (type: summary, error, etc.)
            else:
                # Clear progress line if interactive
                if is_interactive:
                    sys.stdout.write(f"\r{' '*80}\r") 
                    print(f"[{msg_type.upper()}] {json.dumps(data)}")
                
                # Save to S3 log buffer
                captured_logs.append(line)

        except json.JSONDecodeError:
            # Handle non-JSON output (usually critical errors from Go runtime)
            if is_interactive: print(line)
            captured_logs.append(line)

    process.wait()
    return process.returncode, captured_logs

def run_backup_logic():
    load_shell_env(ENV_FILE)
    repo_url = os.environ.get("RESTIC_REPOSITORY")
    kuma_url = os.environ.get("BACKUP_HEALTH_CHECK_URL")
    
    if not repo_url:
        logging.error("No RESTIC_REPOSITORY env var found.")
        return

    bucket_name, endpoint_url = parse_repo_config(repo_url)
    start_time = time.time()
    
    # Setup Log Header
    run_log = [f"Starting backup for host: {HOSTNAME} at {datetime.now()}"]
    if endpoint_url: run_log.append(f"Endpoint: {endpoint_url}")

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

    # --- Run with Monitoring ---
    if sys.stdout.isatty():
        print(f"🚀 Starting Backup to {bucket_name}...")

    return_code, process_logs = monitor_process(cmd, env)
    
    # Merge logs
    run_log.extend(process_logs)
    duration_ms = int((time.time() - start_time) * 1000)

    if return_code == 0:
        msg = f"Backup Successful. Duration: {duration_ms}ms"
        logging.info(msg)
        run_log.append(msg)
        if sys.stdout.isatty(): print(f"\n✅ {msg}")

        if kuma_url:
            try:
                requests.get(f"{kuma_url}{duration_ms}", timeout=10)
            except Exception as e:
                logging.error(f"Kuma Ping Failed: {e}")

        upload_log_to_s3("\n".join(run_log), "SUCCESS", bucket_name, endpoint_url)
    else:
        msg = f"Backup Failed. Code: {return_code}"
        logging.error(msg)
        run_log.append(msg)
        if sys.stdout.isatty(): print(f"\n❌ {msg}")
        
        upload_log_to_s3("\n".join(run_log), "FAILURE", bucket_name, endpoint_url)

def prevent_sleep_and_run():
    script_path = os.path.abspath(__file__)
    # We do NOT use capture_output here so stdin/stdout pass through to terminal
    subprocess.run(["/usr/bin/caffeinate", "-ism", "uv", "run", script_path, "--run-logic"])

if __name__ == "__main__":
    if "--run-logic" in sys.argv:
        run_backup_logic()
    else:
        if check_power():
            prevent_sleep_and_run()
        else:
            logging.info("On Battery. Skipping.")
            if sys.stdout.isatty():
                print("🔋 On Battery. Skipping backup.")