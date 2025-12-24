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
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from urllib.parse import urlparse

# --- Configuration ---
USER_HOME = Path.home()
RESTIC_BIN = "/usr/local/bin/restic"
PASS_FILE = USER_HOME / ".config/restic/password"
EXCLUDE_FILE = USER_HOME / ".config/restic/excludes.txt"
ENV_FILE = USER_HOME / ".config/restic/env"
LOCAL_LOG_FILE = "/tmp/restic_backup.log"

# Sanitize Hostname
RAW_HOSTNAME = socket.gethostname()
HOSTNAME = re.sub(r'[^a-zA-Z0-9-]', '-', RAW_HOSTNAME)
LOGS_DIR_NAME = f"logs/{HOSTNAME}"

# ANSI Escape Code
CLEAR_EOL = "\033[K"

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
    """
    Parses Restic S3 URLs to extract Bucket, Endpoint, AND Prefix.
    Returns: (bucket_name, endpoint_url, repo_prefix)
    
    Example: s3:host/bucket/repo-mac -> (bucket, https://host, repo-mac)
    """
    if not repo_url: return None, None, None
    
    clean_url = repo_url.replace("s3:", "", 1)
    bucket = None
    endpoint = None
    prefix = ""

    # 1. Handle http(s):// prefixes explicitly
    if clean_url.startswith("http"):
        parsed = urlparse(clean_url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}"
        path_parts = parsed.path.strip("/").split("/")
        
        if path_parts:
            bucket = path_parts[0]
            if len(path_parts) > 1:
                prefix = "/".join(path_parts[1:])
        return bucket, endpoint, prefix

    # 2. Handle host/bucket/prefix format (Common for MinIO/Hetzner)
    if "/" in clean_url:
        parts = clean_url.split("/", 1) # Split Host vs Path
        host = parts[0]
        full_path = parts[1] if len(parts) > 1 else ""
        
        path_segments = full_path.split("/")
        if path_segments:
            bucket = path_segments[0]
            # Everything after bucket is the prefix
            if len(path_segments) > 1:
                prefix = "/".join(path_segments[1:])
        
        # Determine Endpoint
        if "amazonaws.com" in host:
            endpoint = None
        else:
            endpoint = f"https://{host}"
            
        return bucket, endpoint, prefix

    # 3. Fallback (AWS Alias: s3:bucketname)
    return clean_url, None, ""

def check_power():
    try:
        result = subprocess.run(["pmset", "-g", "ps"], capture_output=True, text=True)
        return "AC Power" in result.stdout
    except Exception:
        return False

def upload_log_to_s3(content, status_tag, bucket_name, endpoint_url, repo_prefix):
    if not bucket_name: return
    try:
        session = boto3.Session(
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
        
        s3 = session.client('s3', endpoint_url=endpoint_url)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        
        # Construct Key: prefix/logs/hostname/timestamp.txt
        # If prefix is empty, it handles it gracefully
        full_key_path = f"{LOGS_DIR_NAME}/{timestamp}_{status_tag}.txt"
        if repo_prefix:
            full_key_path = f"{repo_prefix}/{full_key_path}"

        s3.put_object(
            Bucket=bucket_name, 
            Key=full_key_path, 
            Body=content, 
            ContentType="text/plain"
        )
        logging.info(f"Uploaded log to s3://{bucket_name}/{full_key_path}")
    except Exception as e:
        logging.error(f"S3 Upload Failed: {e}")

def format_eta(seconds):
    if seconds is None: return "--:--"
    return str(timedelta(seconds=int(seconds)))

def monitor_process(cmd, env):
    process = subprocess.Popen(
        cmd, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.STDOUT, 
        text=True, 
        env=env,
        bufsize=1 
    )

    captured_logs = []
    is_interactive = sys.stdout.isatty()
    term_width = shutil.get_terminal_size((80, 20)).columns if is_interactive else 80

    for line in process.stdout:
        line = line.strip()
        if not line: continue

        try:
            data = json.loads(line)
            msg_type = data.get("message_type")

            if msg_type == "status":
                if is_interactive:
                    percent = data.get("percent_done", 0.0) * 100
                    seconds_rem = data.get("seconds_remaining")
                    eta = format_eta(seconds_rem)
                    
                    current_files = data.get("current_files", [])
                    current_file = current_files[0] if current_files else ""
                    
                    prefix = f"[{percent:5.1f}%] ETA: {eta} | "
                    max_len = term_width - len(prefix) - 2
                    
                    if len(current_file) > max_len and max_len > 0:
                        current_file = "..." + current_file[-(max_len-3):]
                    
                    sys.stdout.write(f"\r{prefix}{current_file}{CLEAR_EOL}")
                    sys.stdout.flush()
            else:
                if is_interactive:
                    sys.stdout.write(f"\r{CLEAR_EOL}") 
                    if msg_type == "summary":
                        print(f"✅ Backup Summary: {data.get('files_new', 0)} new files, {data.get('data_added', 0)/1024/1024:.2f} MB added.")
                    elif msg_type == 'error':
                         print(f"❌ Error: {data.get('error', {}).get('message', 'Unknown')}")
                    else:
                         print(f"[{msg_type.upper()}] {json.dumps(data)}")
                captured_logs.append(line)

        except json.JSONDecodeError:
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

    bucket_name, endpoint_url, repo_prefix = parse_repo_config(repo_url)
    start_time = time.time()
    
    run_log = [f"Starting backup for host: {HOSTNAME} at {datetime.now()}"]
    if endpoint_url: run_log.append(f"Endpoint: {endpoint_url}")
    if repo_prefix: run_log.append(f"Prefix: {repo_prefix}")

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

    if sys.stdout.isatty():
        print(f"🚀 Starting Backup to {bucket_name}/{repo_prefix if repo_prefix else ''} ...")

    return_code, process_logs = monitor_process(cmd, env)
    
    run_log.extend(process_logs)
    duration_ms = int((time.time() - start_time) * 1000)

    if return_code == 0:
        msg = f"Backup Successful. Duration: {duration_ms}ms"
        logging.info(msg)
        run_log.append(msg)
        if sys.stdout.isatty(): print(f"\n✨ {msg}")

        if kuma_url:
            try:
                requests.get(f"{kuma_url}{duration_ms}", timeout=10)
            except Exception as e:
                logging.error(f"Kuma Ping Failed: {e}")

        upload_log_to_s3("\n".join(run_log), "SUCCESS", bucket_name, endpoint_url, repo_prefix)
    else:
        msg = f"Backup Failed. Code: {return_code}"
        logging.error(msg)
        run_log.append(msg)
        if sys.stdout.isatty(): print(f"\n🔥 {msg}")
        
        upload_log_to_s3("\n".join(run_log), "FAILURE", bucket_name, endpoint_url, repo_prefix)

def prevent_sleep_and_run():
    script_path = os.path.abspath(__file__)
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
