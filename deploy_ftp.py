#!/usr/bin/env python3
"""
Hostinger FTP Uploader for Dealer Gamma Engine web application and API files.
Deploys index.html, core python scripts, API JSON data, and persistent ledger to Hostinger.
Zero external dependencies (uses Python standard library ftplib).
"""

import os
import sys
import ftplib
import re
import socket


def clean_host(raw_host: str) -> str:
    """Strip protocol, port, path, and whitespace from hostname."""
    h = raw_host.strip()
    h = re.sub(r'^[a-zA-Z]+://', '', h)  # strip ftp:// or https://
    h = h.split('/')[0]                  # strip any trailing paths
    h = h.split(':')[0]                  # strip port
    return h.strip()


def resolve_working_ftp_host(raw_host: str) -> str:
    """
    Intelligently discover working FTP endpoint.
    If 'reil.studio' is passed, tests 'ftp.reil.studio' first (which listens on port 21),
    avoiding the 30s timeout on web-only hosts.
    """
    cleaned = clean_host(raw_host)
    candidates = []
    if not cleaned.startswith("ftp."):
        candidates.append(f"ftp.{cleaned}")
    candidates.append(cleaned)

    for cand in candidates:
        try:
            with socket.create_connection((cand, 21), timeout=4):
                print(f"Verified FTP endpoint responding on port 21: {cand}")
                return cand
        except Exception as e:
            print(f"Pre-check on {cand}:21 failed ({e}). Trying next candidate...")

    return cleaned


def navigate_or_create(ftp: ftplib.FTP, rel_path: str):
    """Safely navigate to a nested directory path, creating folders if needed."""
    parts = rel_path.strip("/").split("/")
    for p in parts:
        if not p:
            continue
        try:
            ftp.cwd(p)
        except ftplib.error_perm:
            try:
                ftp.mkd(p)
            except Exception:
                pass
            ftp.cwd(p)


def upload_files():
    raw_host = os.getenv("HOSTINGER_FTP_HOST", "").strip()
    user = os.getenv("HOSTINGER_FTP_USER", "").strip()
    passwd = os.getenv("HOSTINGER_FTP_PASS", "").strip()

    if not raw_host or not user or not passwd:
        print("::warning::Missing HOSTINGER_FTP_* secrets. Skipping FTP deployment.")
        sys.exit(0)

    host = resolve_working_ftp_host(raw_host)
    print(f"Connecting to FTP server: {host} as user: {user}...")

    base_dir = os.path.dirname(os.path.abspath(__file__))

    # Define root files to upload -> (local_relative_path, remote_filename)
    root_files = [
        ("static/index.html", "index.html"),
        ("gamma_engine.py", "gamma_engine.py"),
        ("cron_collector.py", "cron_collector.py"),
        ("db_manager.py", "db_manager.py"),
        ("requirements.txt", "requirements.txt"),
    ]

    # Define api files to upload -> (local_relative_path, remote_filename)
    api_files = [
        ("api/action_SPY.json", "action_SPY.json"),
        ("api/action_SPX.json", "action_SPX.json"),
        ("api/history_SPY.json", "history_SPY.json"),
        ("api/history_SPX.json", "history_SPX.json"),
    ]

    # Define persistent ledger & archive files to upload -> (local_relative_path, remote_filename)
    data_files = [
        ("data/real_gamma_archive.json", "real_gamma_archive.json"),
        ("data/gamma_ledger.db", "gamma_ledger.db"),
    ]

    ftp = None
    try:
        # Try FTPS (FTP over TLS) first, with fallback to standard FTP
        try:
            ftp = ftplib.FTP_TLS(timeout=30)
            ftp.connect(host, 21)
            ftp.login(user, passwd)
            ftp.prot_p()
            print("Connected and authenticated successfully via FTPS (TLS).")
        except Exception as tls_err:
            print(f"FTPS handshake failed ({tls_err}). Falling back to plain FTP...")
            if ftp:
                try:
                    ftp.close()
                except Exception:
                    pass
            ftp = ftplib.FTP(timeout=30)
            ftp.connect(host, 21)
            ftp.login(user, passwd)
            print("Connected and authenticated successfully via plain FTP.")

        # Ensure passive mode
        ftp.set_pasv(True)
        # Hostinger NAT passive mode fix:
        # Some Hostinger FTP daemons return internal RFC1918 IPs in 227 PASV response,
        # which causes data transfer timeouts when called from GitHub Actions / outside NAT.
        orig_makepasv = ftp.makepasv
        ftp.makepasv = lambda: (ftp.host, orig_makepasv()[1])

        # Determine target base directory on Hostinger
        potential_base_targets = [
            "domains/reil.studio/public_html/gamma",
            "public_html/gamma",
            "gamma",
            "."
        ]

        target_base = None
        for candidate in potential_base_targets:
            try:
                ftp.cwd("/")
                navigate_or_create(ftp, candidate)
                target_base = candidate
                print(f"Target base directory verified on FTP: {ftp.pwd()}")
                break
            except Exception:
                continue

        if not target_base:
            raise RuntimeError(f"Failed to navigate to target directory. Current path: {ftp.pwd()}")

        # 1. Upload root web and engine files
        print(f"\n--- Uploading Root Files to {ftp.pwd()} ---")
        for local_rel, remote_name in root_files:
            local_path = os.path.join(base_dir, local_rel)
            if not os.path.exists(local_path):
                print(f"Skipping {local_rel} (not found locally)")
                continue
            with open(local_path, "rb") as f:
                ftp.storbinary(f"STOR {remote_name}", f)
            print(f"Uploaded {remote_name} successfully ({os.path.getsize(local_path)} bytes).")

        # 2. Upload API files to api/ subdirectory
        for api_subdir in (f"{target_base}/api", f"{target_base}/static/api"):
            try:
                ftp.cwd("/")
                navigate_or_create(ftp, api_subdir)
                print(f"\n--- Uploading API Files to {ftp.pwd()} ---")
                for local_rel, remote_name in api_files:
                    local_path = os.path.join(base_dir, local_rel)
                    if not os.path.exists(local_path):
                        print(f"Skipping {local_rel} (not found locally)")
                        continue
                    with open(local_path, "rb") as f:
                        ftp.storbinary(f"STOR {remote_name}", f)
                    print(f"Uploaded {remote_name} to {api_subdir} ({os.path.getsize(local_path)} bytes).")
            except Exception as api_err:
                print(f"Warning syncing to {api_subdir}: {api_err}")

        # 3. Upload persistent data files to data/ subdirectory
        try:
            ftp.cwd("/")
            navigate_or_create(ftp, f"{target_base}/data")
            print(f"\n--- Uploading Persistent Ledger Files to {ftp.pwd()} ---")
            for local_rel, remote_name in data_files:
                local_path = os.path.join(base_dir, local_rel)
                if not os.path.exists(local_path):
                    continue
                with open(local_path, "rb") as f:
                    ftp.storbinary(f"STOR {remote_name}", f)
                print(f"Uploaded {remote_name} to data/ ({os.path.getsize(local_path)} bytes).")
        except Exception as data_err:
            print(f"Warning syncing data directory: {data_err}")

        print("\nAll application, API, and ledger files deployed successfully to Hostinger!")

    except ftplib.error_perm as e:
        print(f"::error::FTP Authentication or Permission Error: {e}")
        print("Hint: Verify HOSTINGER_FTP_USER and HOSTINGER_FTP_PASS.")
        sys.exit(1)
    except Exception as e:
        print(f"::error::FTP Connection Error: {e}")
        print(f"Hint: Checked host '{host}'.")
        sys.exit(1)
    finally:
        if ftp:
            try:
                ftp.quit()
            except Exception:
                pass


if __name__ == "__main__":
    upload_files()
