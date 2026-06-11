#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Load REAL Home Assistant Logbook events into Log Book DB.
NO fake/test data. Everything comes from the actual HA Logbook API.

- Users come from the real person entities (user_id -> name mapping)
- Automations come from the real context_name / context_entity_id
- Devices/entities come from the real entity_id / friendly name
- Messages are the real logbook messages
"""

import requests
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
import sys
import io

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Credentials from env vars or config.json (never hardcoded)
from config import load_config
_CFG = load_config()
HA_URL = _CFG["ha_url"]
HA_TOKEN = _CFG["ha_token"]
DB_PATH = Path.home() / '.homeassistant' / 'log_book.db'

DAYS_BACK = 14

headers = _CFG["headers"]


def get_person_map():
    """Build a {user_id: person_name} map from real person entities."""
    print("  Lade Personen (echte User)...")
    try:
        r = requests.get(f"{HA_URL}/api/states", headers=headers, timeout=30)
        r.raise_for_status()
        states = r.json()
        person_map = {}
        for s in states:
            if s["entity_id"].startswith("person."):
                attrs = s.get("attributes", {})
                uid = attrs.get("user_id")
                name = attrs.get("friendly_name", s["entity_id"].split(".")[1])
                if uid:
                    person_map[uid] = name
        print(f"    OK - {len(person_map)} echte User gemappt")
        return person_map
    except Exception as e:
        print(f"    FEHLER: {e}")
        return {}


def get_logbook():
    """Fetch the real HA logbook day by day for the last DAYS_BACK days."""
    print(f"  Lade echtes Logbook ({DAYS_BACK} Tage, tageweise)...")
    all_entries = []
    now = datetime.now()
    for d in range(DAYS_BACK):
        day_start = (now - timedelta(days=d + 1)).replace(microsecond=0).isoformat()
        day_end = (now - timedelta(days=d)).replace(microsecond=0).isoformat()
        url = f"{HA_URL}/api/logbook/{day_start}?end_time={day_end}"
        try:
            r = requests.get(url, headers=headers, timeout=120)
            r.raise_for_status()
            data = r.json()
            all_entries.extend(data)
            print(f"    Tag -{d+1}: {len(data)} Einträge (gesamt {len(all_entries)})")
        except Exception as e:
            print(f"    Tag -{d+1}: FEHLER {e}")
    print(f"    OK - {len(all_entries)} echte Logbook-Einträge gesamt")
    return all_entries


def normalize_timestamp(when):
    """Convert HA ISO timestamp (with tz) to local naive ISO for DB consistency."""
    try:
        dt = datetime.fromisoformat(when)
        # Convert to local time, drop tzinfo so it matches existing DB format
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.isoformat()
    except Exception:
        return when


def build_metadata(entry, person_map):
    """Extract the HA context fields needed to reconstruct the process chain."""
    ctx_user_id = entry.get("context_user_id")
    return {
        "context_id": entry.get("context_id"),
        "context_event_type": entry.get("context_event_type"),
        "context_domain": entry.get("context_domain"),
        "context_name": entry.get("context_name"),
        "context_entity_id": entry.get("context_entity_id"),
        "context_entity_id_name": entry.get("context_entity_id_name"),
        "context_source": entry.get("context_source"),
        "context_message": entry.get("context_message"),
        "context_user_id": ctx_user_id,
        "context_user_name": person_map.get(ctx_user_id) if ctx_user_id else None,
        "state": entry.get("state"),
    }


def classify(entry, person_map):
    """Derive (event_type, user, device, entity, automation, message) from a real logbook entry."""
    entity_id = entry.get("entity_id")
    name = entry.get("name", entity_id or "Unbekannt")
    state = entry.get("state")
    raw_message = entry.get("message")
    domain = entity_id.split(".")[0] if entity_id else None

    # person.* entities are USERS (presence), not devices
    if domain == "person":
        message = raw_message and f"{name} {raw_message}" or f"{name}: {state}"
        return "user_action", name, None, entity_id, None, message

    # Device = friendly entity name (real)
    device = name
    entity = entity_id

    # Automation context (real)
    automation = None
    ctx_domain = entry.get("context_domain")
    ctx_name = entry.get("context_name")
    if ctx_domain == "automation" and ctx_name:
        automation = ctx_name

    # User context (real, mapped from person entities)
    user = None
    ctx_user_id = entry.get("context_user_id")
    if ctx_user_id and ctx_user_id in person_map:
        # Only attribute to user when NOT an automation-driven event
        if ctx_domain != "automation":
            user = person_map[ctx_user_id]

    # Event type
    if automation or entry.get("context_event_type") == "automation_triggered":
        event_type = "automation_triggered"
    elif domain == "automation":
        event_type = "automation_triggered"
    elif user:
        event_type = "user_action"
    else:
        event_type = "state_change"

    # Message (real)
    if raw_message:
        message = f"{name} {raw_message}"
    elif state is not None:
        message = f"{name}: {state}"
    else:
        message = name

    return event_type, user, device, entity, automation, message


def main():
    print("=" * 70)
    print("Home Assistant - ECHTE Logbook Daten laden")
    print("=" * 70)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("\nAbrufen von echten Daten aus Home Assistant...")
    person_map = get_person_map()
    logbook = get_logbook()

    if not logbook:
        print("FEHLER: Kein Logbook geladen!")
        return

    # Clear old logs
    print("\nLösche alte (Fake-)Logs...")
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    cursor.execute("DELETE FROM logs")
    conn.commit()

    print(f"\nVerarbeite {len(logbook)} echte Einträge...\n")
    total = 0
    for i, entry in enumerate(logbook):
        event_type, user, device, entity, automation, message = classify(entry, person_map)
        timestamp = normalize_timestamp(entry.get("when"))
        metadata = build_metadata(entry, person_map)

        cursor.execute(
            """INSERT INTO logs (timestamp, event_type, message, user, device, entity, automation, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, event_type, message, user, device, entity, automation, json.dumps(metadata))
        )
        total += 1

        if (i + 1) % 2000 == 0:
            conn.commit()
            print(f"  {i + 1}/{len(logbook)} verarbeitet...")

    conn.commit()

    # Stats
    cursor.execute("SELECT DISTINCT event_type FROM logs ORDER BY event_type")
    event_types = [r[0] for r in cursor.fetchall()]
    cursor.execute("SELECT DISTINCT user FROM logs WHERE user IS NOT NULL ORDER BY user")
    users = [r[0] for r in cursor.fetchall()]
    cursor.execute("SELECT DISTINCT automation FROM logs WHERE automation IS NOT NULL ORDER BY automation")
    automations = [r[0] for r in cursor.fetchall()]
    cursor.execute("SELECT COUNT(DISTINCT device) FROM logs WHERE device IS NOT NULL")
    device_count = cursor.fetchone()[0]
    conn.close()

    print(f"\n" + "=" * 70)
    print(f"FERTIG! {total} ECHTE Logs geladen")
    print(f"  Event-Typen: {event_types}")
    print(f"  Echte User: {users}")
    print(f"  Echte Automationen ({len(automations)}): {automations}")
    print(f"  Geräte: {device_count}")
    print(f"=" * 70)
    print(f"\nSeite neu laden: http://localhost:8080")


if __name__ == "__main__":
    main()
