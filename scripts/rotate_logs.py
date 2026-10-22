#!/usr/bin/env python3
"""
Time-based log rotation and archiving for plex_debrid.
Retention Policy:
  - Active log (plex_debrid.log): keeps last 30 days of entries.
  - Archive log (plex_debrid.log.archive): keeps prior 6 months (30 days to 180 days).
  - Purge: anything older than 6 months (180 days) is deleted.
  - In-place log rewrites preserve launchd open file descriptors.
  - Collapses consecutive runaway duplicate error bursts to prevent disk bloat.
"""

import os
import sys
import re
from datetime import datetime, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(SCRIPT_DIR)

PLEX_LOG = os.path.join(BASE_DIR, "plex_debrid.log")
PLEX_ARCHIVE = os.path.join(BASE_DIR, "plex_debrid.log.archive")

SYNC_LOG = os.path.join(BASE_DIR, "sync_maintenance.log")
SYNC_ARCHIVE = os.path.join(BASE_DIR, "sync_maintenance.log.archive")

TS_REGEX = re.compile(r'^\[(\d{2})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\]')
SYNC_TS_REGEX = re.compile(r'(\d{4}-\d{2}-\d{2})')

def collapse_consecutive_duplicates(lines, threshold=3):
    """
    Collapses long streaks of identical error messages to keep logs clean and readable.
    """
    if not lines:
        return []
    
    result = []
    prev_content = None
    dup_count = 0
    first_dup_line = None

    def strip_ts(line):
        # Strip timestamp prefix if present to compare message content
        m = TS_REGEX.match(line)
        if m:
            return line[m.end():].strip()
        return line.strip()

    for line in lines:
        content = strip_ts(line)
        if content == prev_content and content:
            dup_count += 1
            if dup_count <= threshold:
                result.append(line)
        else:
            if dup_count > threshold:
                result.append(f"    ... [Previous message repeated {dup_count - threshold} additional times] ...\n")
            prev_content = content
            dup_count = 1
            result.append(line)

    if dup_count > threshold:
        result.append(f"    ... [Previous message repeated {dup_count - threshold} additional times] ...\n")

    return result

def rotate_plex_log(collapse_repeats=True):
    if not os.path.exists(PLEX_LOG):
        return

    now = datetime.now()
    cutoff_active = now - timedelta(days=30)
    cutoff_archive = now - timedelta(days=180)

    # 1. Read existing active log
    with open(PLEX_LOG, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    if not lines:
        return

    active_lines = []
    new_archive_lines = []
    
    # We will determine date per line. Lines without a timestamp inherit from previous line.
    current_dt = None
    # If the file starts without timestamps, scan forward to find the first timestamp
    for l in lines:
        m = TS_REGEX.match(l)
        if m:
            try:
                current_dt = datetime.strptime(m.group(1), "%d/%m/%y %H:%M:%S")
                break
            except Exception:
                pass
    if current_dt is None:
        current_dt = now

    for line in lines:
        m = TS_REGEX.match(line)
        if m:
            try:
                current_dt = datetime.strptime(m.group(1), "%d/%m/%y %H:%M:%S")
            except Exception:
                pass

        if current_dt >= cutoff_active:
            active_lines.append(line)
        elif current_dt >= cutoff_archive:
            new_archive_lines.append(line)
        # older than cutoff_archive is discarded

    if collapse_repeats:
        active_lines = collapse_consecutive_duplicates(active_lines)
        new_archive_lines = collapse_consecutive_duplicates(new_archive_lines)

    # 2. Update active log in-place so open launchd file descriptors continue smoothly
    with open(PLEX_LOG, "r+", encoding="utf-8", errors="replace") as f:
        f.seek(0)
        f.writelines(active_lines)
        f.truncate()

    # 3. Read and prune existing archive log (keep only up to 180 days)
    existing_archive_lines = []
    if os.path.exists(PLEX_ARCHIVE):
        with open(PLEX_ARCHIVE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = TS_REGEX.match(line)
                if m:
                    try:
                        current_dt = datetime.strptime(m.group(1), "%d/%m/%y %H:%M:%S")
                    except Exception:
                        pass
                if current_dt >= cutoff_archive:
                    existing_archive_lines.append(line)

    combined_archive = existing_archive_lines + new_archive_lines
    if combined_archive:
        with open(PLEX_ARCHIVE, "w", encoding="utf-8", errors="replace") as f:
            f.writelines(combined_archive)

    orig_count = len(lines)
    retained_count = len(active_lines)
    archived_count = len(new_archive_lines)
    print(f"[Log Rotation] plex_debrid.log: {orig_count} -> {retained_count} lines retained (30d). Archived: {archived_count} lines.")

def rotate_sync_log():
    if not os.path.exists(SYNC_LOG):
        return

    now = datetime.now()
    cutoff_active = now - timedelta(days=30)
    cutoff_archive = now - timedelta(days=180)

    with open(SYNC_LOG, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    if not lines:
        return

    active_lines = []
    new_archive_lines = []
    current_dt = now

    for line in lines:
        m = SYNC_TS_REGEX.search(line)
        if m:
            try:
                current_dt = datetime.strptime(m.group(1), "%Y-%m-%d")
            except Exception:
                pass

        if current_dt >= cutoff_active:
            active_lines.append(line)
        elif current_dt >= cutoff_archive:
            new_archive_lines.append(line)

    with open(SYNC_LOG, "r+", encoding="utf-8", errors="replace") as f:
        f.seek(0)
        f.writelines(active_lines)
        f.truncate()

    existing_archive_lines = []
    if os.path.exists(SYNC_ARCHIVE):
        with open(SYNC_ARCHIVE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = SYNC_TS_REGEX.search(line)
                if m:
                    try:
                        current_dt = datetime.strptime(m.group(1), "%Y-%m-%d")
                    except Exception:
                        pass
                if current_dt >= cutoff_archive:
                    existing_archive_lines.append(line)

    combined_archive = existing_archive_lines + new_archive_lines
    if combined_archive:
        with open(SYNC_ARCHIVE, "w", encoding="utf-8", errors="replace") as f:
            f.writelines(combined_archive)

    print(f"[Log Rotation] sync_maintenance.log: {len(lines)} -> {len(active_lines)} lines retained (30d).")

def rotate_all():
    rotate_plex_log()
    rotate_sync_log()

if __name__ == "__main__":
    rotate_all()
