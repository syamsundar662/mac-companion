#!/bin/zsh
# Run Mac from ~/Applications/Mac.app at login, restarted after a crash. Undo with ./uninstall_login.sh.
set -e
cd "${0:A:h}"
LABEL=local.mac-companion
PLIST=~/Library/LaunchAgents/$LABEL.plist
[[ -x ~/Applications/Mac.app/Contents/MacOS/Mac ]] || ./build_app.sh
# Stop the start.sh copy and wait for it to exit, so two copies never fight over the mic.
pid=$(cat .lock 2>/dev/null || true)
./stop.sh
for i in {1..50}; do [[ -n $pid ]] && kill -0 $pid 2>/dev/null || break; sleep 0.2; done
for old in ~/Library/LaunchAgents/*mac-companion.plist(N); do  # an earlier install, maybe under an older label
  launchctl bootout gui/$(id -u)/${old:t:r} 2>/dev/null || true
  rm -f $old
done
mkdir -p ~/Library/LaunchAgents
# Starts Mac.app at login and restarts it after a crash (Quit from the menu stays quit).
cat > $PLIST <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>$LABEL</string>
	<key>ProgramArguments</key><array><string>$HOME/Applications/Mac.app/Contents/MacOS/Mac</string></array>
	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
	<key>ThrottleInterval</key><integer>5</integer>
	<key>LimitLoadToSessionType</key><string>Aqua</string>
	<key>ProcessType</key><string>Interactive</string>
	<key>StandardOutPath</key><string>$PWD/companion.log</string>
	<key>StandardErrorPath</key><string>$PWD/companion.log</string>
</dict>
</plist>
EOF
launchctl bootstrap gui/$(id -u) $PLIST
echo "Mac.app is running and starts at login. Allow the microphone, then add Mac to Accessibility and"
echo "Screen & System Audio Recording in System Settings > Privacy & Security."
