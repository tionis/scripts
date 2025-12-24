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

# State management for "Once Daily"
STATE_DIR = USER_HOME / ".local/state/restic"
LAST_RUN_FILE = STATE_DIR / "last_successful_run"

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
    """
    if not repo_url: return None, None, None
    
    clean_url = repo_url.replace("s3:", "", 1)
    bucket = None
    endpoint = None
    prefix = ""

    if clean_url.startswith("http"):
        parsed = urlparse(clean_url)
        endpoint = f"{parsed.scheme}://{parsed.netloc}"
        path_parts = parsed.path.strip("/").split("/")
        if path_parts:
            bucket = path_parts[0]
            if len(path_parts) > 1:
                prefix = "/".join(path_parts[1:])
        return bucket, endpoint, prefix

    if "/" in clean_url:
        parts = clean_url.split("/", 1)
        host = parts[0]
        full_path = parts[1] if len(parts) > 1 else ""
        
        path_segments = full_path.split("/")
        if path_segments:
            bucket = path_segments[0]
            if len(path_segments) > 1:
                prefix = "/".join(path_segments[1:])
        
        if "amazonaws.com" in host: endpoint = None
        else: endpoint = f"https://{host}"
            
        return bucket, endpoint, prefix

    return clean_url, None, ""

def check_power():
    try:
        result = subprocess.run(["pmset", "-g", "ps"], capture_output=True, text=True)
        return "AC Power" in result.stdout
    except Exception:
        return False

def check_last_run():
    """Returns True if backup should run (last run > 20 hours ago)."""
    if not LAST_RUN_FILE.exists():
        return True
    
    try:
        mtime = datetime.fromtimestamp(LAST_RUN_FILE.stat().st_mtime)
        # Using 20 hours to allow for slight schedule drift without skipping a day
        if datetime.now() - mtime < timedelta(hours=20):
            return False
        return True
    except Exception:
        return True

def update_last_run():
    """Touches the state file to mark success."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_RUN_FILE.touch()
    except Exception as e:
        logging.error(f"Failed to update state file: {e}")

def upload_log_to_s3(content, status_tag, bucket_name, endpoint_url, repo_prefix):
    if not bucket_name: return
    try:
        session = boto3.Session(
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
        
        s3 = session.client('s3', endpoint_url=endpoint_url)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        
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

def monitor_process(cmd, env, context="Backup"):
    """
    Generic monitor for both Backup and Prune operations.
    """
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
                    
                    # Prune operations sometimes don't send seconds_remaining
                    eta = format_eta(seconds_rem) if seconds_rem is not None else "??"
                    
                    current_files = data.get("current_files", [])
                    current_file = current_files[0] if current_files else ""
                    
                    # Context prefix (Backup/Prune)
                    prefix = f"[{context}] [{percent:5.1f}%] ETA: {eta} | "
                    max_len = term_width - len(prefix) - 2
                    
                    if len(current_file) > max_len and max_len > 0:
                        current_file = "..." + current_file[-(max_len-3):]
                    
                    sys.stdout.write(f"\r{prefix}{current_file}{CLEAR_EOL}")
                    sys.stdout.flush()
            else:
                if is_interactive:
                    sys.stdout.write(f"\r{CLEAR_EOL}") 
                    if msg_type == "summary":
                        # Backup Summary
                        if "data_added" in data:
                            print(f"✅ {context} Summary: {data.get('files_new', 0)} new files, {data.get('data_added', 0)/1024/1024:.2f} MB added.")
                        # Prune/Forget Summary
                        elif "keep" in data:
                            print(f"✂️ {context} Complete. Snapshots Kept: {len(data.get('keep', []))}, Removed: {len(data.get('remove', []))}")
                    elif msg_type == 'error':
                         print(f"❌ Error: {data.get('error', {}).get('message', 'Unknown')}")
                    else:
                         # Just print unknown JSON types cleanly
                         pass 
                captured_logs.append(line)

        except json.JSONDecodeError:
            if is_interactive: print(line)
            captured_logs.append(line)

    process.wait()
    return process.returncode, captured_logs

def run_backup_logic(force=False):
    load_shell_env(ENV_FILE)
    repo_url = os.environ.get("RESTIC_REPOSITORY")
    kuma_url = os.environ.get("BACKUP_HEALTH_CHECK_URL")
    
    if not repo_url:
        logging.error("No RESTIC_REPOSITORY env var found.")
        return

    # 1. Daily Frequency Check
    if not force and not check_last_run():
        logging.info("Backup skipped (Ran < 20 hours ago).")
        if sys.stdout.isatty():
            print("⏭️  Backup skipped (Ran < 20 hours ago). Use --force to override.")
        return

    bucket_name, endpoint_url, repo_prefix = parse_repo_config(repo_url)
    start_time = time.time()
    
    # Initialize Log Buffer
    run_log = [f"Starting Run: {HOSTNAME} at {datetime.now()}", f"Repo: {repo_url}"]

    # --- Step 1: BACKUP ---
    cmd_backup = [
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
        print(f"🚀 Starting Backup...")

    ret_backup, log_backup = monitor_process(cmd_backup, env, context="Backup")
    run_log.append("\n--- BACKUP LOGS ---")
    run_log.extend(log_backup)

    if ret_backup != 0:
        # FAIL
        logging.error(f"Backup Failed with code {ret_backup}")
        upload_log_to_s3("\n".join(run_log), "FAILURE", bucket_name, endpoint_url, repo_prefix)
        return

    # --- Step 2: PRUNE (Only if backup succeeded) ---
    if sys.stdout.isatty():
        print(f"\n🧹 Starting Prune (Ret: 7d/4w/12m/5y)...")

    cmd_prune = [
        RESTIC_BIN, "forget",
        "--repo", repo_url,
        "--password-file", str(PASS_FILE),
        "--prune",
        "--keep-daily", "7",
        "--keep-weekly", "4",
        "--keep-monthly", "12",
        "--keep-yearly", "5",
        "--tag", "macos", # Only prune this machine's history if desired, or remove to prune globally
        "--host", HOSTNAME,
        "--json"
    ]

    ret_prune, log_prune = monitor_process(cmd_prune, env, context="Prune")
    run_log.append("\n--- PRUNE LOGS ---")
    run_log.extend(log_prune)

    # --- Conclusion ---
    duration_ms = int((time.time() - start_time) * 1000)
    
    # We consider it a "Success" if the Backup worked, even if Prune failed (though we log it)
    status_tag = "SUCCESS" if ret_prune == 0 else "WARNING_PRUNE_FAIL"
    
    msg = f"Operation Finished. Backup: OK. Prune: {'OK' if ret_prune == 0 else 'FAIL'}. Duration: {duration_ms}ms"
    logging.info(msg)
    run_log.append(f"\n{msg}")

    # Mark as run for today
    update_last_run()

    if sys.stdout.isatty():
        print(f"\n✨ {msg}")

    if kuma_url:
        try:
            requests.get(f"{kuma_url}{duration_ms}", timeout=10)
        except Exception as e:
            logging.error(f"Kuma Ping Failed: {e}")

    upload_log_to_s3("\n".join(run_log), status_tag, bucket_name, endpoint_url, repo_prefix)

def prevent_sleep_and_run(force=False):
    script_path = os.path.abspath(__file__)
    cmd = ["/usr/bin/caffeinate", "-ism", "uv", "run", script_path, "--run-logic"]
    if force:
        cmd.append("--force")
    subprocess.run(cmd)

if __name__ == "__main__":
    force_run = "--force" in sys.argv
    
    if "--run-logic" in sys.argv:
        run_backup_logic(force=force_run)
    else:
        # Wrapper Check
        # If we are running automatically (no force), check state BEFORE caffeinate
        # to save creating a caffeinate process if we are just going to exit anyway.
        if not force_run and not check_power():
            logging.info("On Battery. Skipping.")
            if sys.stdout.isatty(): print("🔋 On Battery. Skipping.")
            sys.exit(0)
            
        # We do the frequency check inside `run_backup_logic`, but we could do a quick check here 
        # to avoid launching uv/caffeinate, but doing it inside logs better.
        prevent_sleep_and_run(force=force_run)
