#!/bin/zsh
# Launchd wrapper for the OSIRIS globe collector. Sources the proxy / blob / AIS
# secrets, then execs the persistent collector. Managed by
# ~/Library/LaunchAgents/com.sil.globe-collector.plist (RunAtLoad + KeepAlive),
# so it starts on login and restarts if it ever crashes.
set -a
[ -f "$HOME/.proxyrack.env" ] && . "$HOME/.proxyrack.env"
[ -f "/Users/office/Documents/Claude/Projects/sil-globe/.env.local" ] && . "/Users/office/Documents/Claude/Projects/sil-globe/.env.local"
[ -f "$HOME/.secrets/aisstream.env" ] && . "$HOME/.secrets/aisstream.env"
set +a
cd /Users/office/Desktop/recon-out/osiris-globe-collector
exec /opt/homebrew/opt/python@3.12/bin/python3.12 -B collector.py
