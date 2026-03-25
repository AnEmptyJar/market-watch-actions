# monitor_system (PRODUCTION)

## Components
- scripts/network_watch_pro.py : network monitoring + Telegram alerts + Telegram status command
- config/config.json : configuration (telegram.bot_token / telegram.chat_id)
- LaunchAgent: ~/Library/LaunchAgents/com.jimmy.network_watch.plist

## Verify
- Compile: python3 -m py_compile ~/monitor_system/scripts/network_watch_pro.py
- Run once: launchctl kickstart -k "gui/$(id -u)/com.jimmy.network_watch"
- Logs: tail -n 50 ~/monitor_system/logs/network_watch_pro_launchd.log
- Telegram:
  - alert: automatic on incidents
  - status: send "status" or "/status" then run kickstart (or wait for schedule)

## Restore (quick)
1) Put folder back to ~/monitor_system
2) Install plist:
   cp ~/monitor_system/PRODUCTION_LOCK_*/com.jimmy.network_watch.plist ~/Library/LaunchAgents/
3) Bootstrap:
   launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.jimmy.network_watch.plist
   launchctl kickstart -k "gui/$(id -u)/com.jimmy.network_watch"
