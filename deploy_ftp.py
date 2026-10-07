#!/usr/bin/env python3
"""
Hostinger FTP Uploader for Dealer Gamma Engine API files.
Zero external dependencies (uses Python standard library ftplib).
"""

import os
import sys
import ftplib
import re

def clean_host(raw_host: str) -> str:
    """Strip protocol, port, path, and whitespace from hostname."""
    h = raw_host.strip()
    h = re.sub(r'^[a-zA-Z]+://', '', h)  # strip ftp:// or https://
    h = h.split('/')[0]                  # strip any trailing paths
    h = h.split(':')[0]                  # strip port
    return h.strip()

def upload_files():
    raw_host = os.getenv("HOSTINGER_FTP_HOST", "").strip()
    user = os.getenv("HOSTINGER_FTP_USER", "").strip()
    passwd = os.getenv("HOSTINGER_FTP_PASS", "").strip()

    if not raw_host or not user or not passwd:
        print("::warning::Missing HOSTINGER_FTP_* secrets. Skipping FTP deployment.")
        sys.exit(0)

    host = clean_host(raw_host)
    print(f"Connecting to FTP server: {host} as user: {user}...")

    # Files to upload from api/ directory
    base_dir = os.path.dirname(os.path.abspath(__file__))
    api_dir = os.path.join(base_dir, "api")
    files_to_upload = [
        "action_SPY.json",
        "action_SPX.json",
        "history_SPY.json",
        "history_SPX.json",
    ]

    ftp = None
    try:
        ftp = ftplib.FTP()
        ftp.connect(host, 21, timeout=30)
        ftp.login(user, passwd)
        ftp.set_pasv(True)
        print("Connected and authenticated successfully.")

        # Determine target directory
        # Hostinger root might be / or /domains/reil.studio/public_html/
        potential_targets = [
            "domains/reil.studio/public_html/gamma/api",
            "public_html/gamma/api",
            "gamma/api",
            "api"
        ]

        target_dir = None
        for candidate in potential_targets:
            try:
                ftp.cwd("/")
                parts = candidate.strip("/").split("/")
                for p in parts:
                    try:
                        ftp.cwd(p)
                    except ftplib.error_perm:
                        ftp.mkd(p)
                        ftp.cwd(p)
                target_dir = candidate
                print(f"Target directory verified on FTP: {ftp.pwd()}")
                break
            except Exception as e:
                continue

        if not target_dir:
            print("Failed to navigate to target directory. Current FTP path: " + ftp.pwd())

        # Upload files
        for fname in files_to_upload:
            local_path = os.path.join(api_dir, fname)
            if not os.path.exists(local_path):
                print(f"Skipping {fname} (not found locally in {api_dir})")
                continue
            with open(local_path, "rb") as f:
                ftp.storbinary(f"STOR {fname}", f)
            print(f"Uploaded {fname} successfully ({os.path.getsize(local_path)} bytes).")

        print("\nAll files deployed successfully to Hostinger!")

    except ftplib.error_perm as e:
        print(f"::error::FTP Authentication or Permission Error: {e}")
        print("Hint: Verify HOSTINGER_FTP_USER and HOSTINGER_FTP_PASS in GitHub Secrets.")
        sys.exit(1)
    except Exception as e:
        print(f"::error::FTP Connection Error: {e}")
        print(f"Hint: Checked host '{host}'. Ensure host is 'ftp.reil.studio' or server IP.")
        sys.exit(1)
    finally:
        if ftp:
            try:
                ftp.quit()
            except Exception:
                pass

if __name__ == "__main__":
    upload_files()
