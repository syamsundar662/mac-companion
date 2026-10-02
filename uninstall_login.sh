#!/bin/zsh
# Stop Mac.app and stop it starting at login. ./start.sh still works afterwards.
plists=(~/Library/LaunchAgents/*mac-companion.plist(N))  # this label, or an older one
(( $#plists )) || { echo "Mac.app wasn't running from login."; exit 0; }
for p in $plists; do
  launchctl bootout gui/$(id -u)/${p:t:r} 2>/dev/null && echo "Mac.app stopped."
  rm -f $p
done
echo "Removed the login item. Start Mac the old way with ./start.sh."
