#!/bin/sh
# Keep the WhatsApp bridge running 24/7 as a macOS LaunchAgent (starts at login, restarts after crashes).
#
#   ./service.sh install    build the binary and start the agent
#   ./service.sh uninstall  stop and remove the agent (the MCP server goes back to on-demand starts)
#   ./service.sh restart    rebuild and restart (after changing the Go sources)
#   ./service.sh status     show whether the agent is loaded and the bridge is connected
#
# The MCP server finds the running bridge through store/bridge.sock and does not start its own.
# The bridge is never restarted after an account restriction (exit 3) or when it is not paired (exit 4):
# both need a human. Pair first in a terminal with `go run .`.
set -eu

LABEL="local.whatsapp-mcp.bridge"
DIR="$(cd "$(dirname "$0")" && pwd -P)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
EXIT_RESTRICTED=3
EXIT_NOT_PAIRED=4

build() {
	GO="$(command -v go || true)"
	for c in /opt/homebrew/bin/go /usr/local/go/bin/go /usr/local/bin/go; do
		[ -n "$GO" ] && break
		[ -x "$c" ] && GO="$c"
	done
	[ -n "$GO" ] || { echo "Go not found: install it or build bin/whatsapp-bridge yourself" >&2; exit 1; }
	(cd "$DIR" && "$GO" build -o bin/whatsapp-bridge .)
}

write_plist() {
	mkdir -p "$HOME/Library/LaunchAgents"
	cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>/bin/sh</string>
		<string>$DIR/service.sh</string>
		<string>run</string>
	</array>
	<key>WorkingDirectory</key><string>$DIR</string>
	<key>RunAtLoad</key><true/>
	<!-- Restart only after a failure: run exits 0 for a restriction or a missing pairing -->
	<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
	<key>ThrottleInterval</key><integer>30</integer>
	<key>Umask</key><integer>63</integer>
	<key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF
}

case "${1:-}" in
run)
	# Called by launchd. No idle timeout, no exit-with-parent, never a QR code.
	cd "$DIR"
	mkdir -p store && chmod 700 store
	unset WHATSAPP_IDLE_TIMEOUT WHATSAPP_EXIT_WITH_PARENT
	code=0
	WHATSAPP_NO_PAIRING=1 ./bin/whatsapp-bridge </dev/null >>store/bridge.log 2>&1 || code=$?
	if [ "$code" -eq "$EXIT_RESTRICTED" ] || [ "$code" -eq "$EXIT_NOT_PAIRED" ]; then
		echo "[service] bridge exited with code $code: not restarting, this needs a human (see AGENTS.md)" >>store/bridge.log
		exit 0
	fi
	exit "$code"
	;;
install)
	[ -e "$DIR/store/ACCOUNT_RESTRICTED" ] && { echo "store/ACCOUNT_RESTRICTED exists: not installing" >&2; exit 1; }
	build
	write_plist
	launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
	launchctl bootstrap "$DOMAIN" "$PLIST"
	echo "Installed $PLIST: the bridge now runs at login and restarts after crashes"
	;;
uninstall)
	launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
	rm -f "$PLIST"
	echo "Removed $LABEL"
	;;
restart)
	build
	launchctl kickstart -k "$DOMAIN/$LABEL"
	echo "Restarted $LABEL"
	;;
status)
	launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E '^\s*(state|pid|last exit code) ' || echo "$LABEL is not loaded"
	tail -n 5 "$DIR/store/bridge.log" 2>/dev/null || true
	;;
*)
	sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
	exit 2
	;;
esac
