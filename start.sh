#!/bin/zsh
# Start the companion in the background. It keeps running after you close this window, and a watchdog
# brings it back if it ever crashes or gets killed. Quit from the menu bar (or ./stop.sh) stops it for good.
cd "${0:A:h}"
if [[ -f .lock ]] && kill -0 $(cat .lock) 2>/dev/null; then
  echo "Companion is already running."
  exit 0
fi
nohup zsh -c '
  quick=0
  while true; do
    started=$(date +%s)
    .venv/bin/python companion.py
    code=$?
    [[ $code -eq 0 ]] && break                                   # quit on purpose
    (( $(date +%s) - started < 30 )) && (( quick++ )) || quick=0
    if (( quick >= 3 )); then
      echo "$(date "+%Y-%m-%d %H:%M:%S") watchdog: crashed 3 times right after starting, giving up. See above."
      break
    fi
    echo "$(date "+%Y-%m-%d %H:%M:%S") watchdog: companion stopped unexpectedly (exit $code), restarting in 3 s"
    sleep 3
  done' >> companion.log 2>&1 &
echo $! > .watchdog
echo "Companion started. Tap ⌃⌥Space to talk. Stop it with ./stop.sh or the menu bar icon > Quit."
