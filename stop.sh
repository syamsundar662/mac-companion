#!/bin/zsh
cd "${0:A:h}"
# Stop the watchdog first so it doesn't bring the companion straight back.
[[ -f .watchdog ]] && kill $(cat .watchdog) 2>/dev/null; rm -f .watchdog
pid=$(cat .lock 2>/dev/null)
[[ -n $pid ]] && kill $pid 2>/dev/null && echo "Companion stopped." || echo "Companion wasn't running."
