#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Configuration loader for the Log Book standalone server / tools.

Credentials are read (in order of priority) from:
  1. Environment variables: HA_URL, HA_TOKEN, HA_WS_URL
  2. A local `config.json` next to this file (gitignored - never commit it!)
  3. Sensible defaults

Create your own config by copying `config.example.json` to `config.json`
and pasting your Home Assistant URL + a Long-Lived Access Token.
"""

import os
import json
from pathlib import Path

_DEFAULT_URL = "http://homeassistant.local:8123"


def load_config():
    cfg = {}
    cfg_path = Path(__file__).parent / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}

    ha_url = (os.environ.get("HA_URL") or cfg.get("ha_url") or _DEFAULT_URL).rstrip("/")
    ha_token = os.environ.get("HA_TOKEN") or cfg.get("ha_token") or ""

    ha_ws = os.environ.get("HA_WS_URL") or cfg.get("ha_ws_url")
    if not ha_ws:
        ha_ws = ha_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"

    return {
        "ha_url": ha_url,
        "ha_token": ha_token,
        "ha_ws_url": ha_ws,
        "headers": {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"},
    }
