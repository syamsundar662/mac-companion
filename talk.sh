#!/bin/zsh
# Same as tapping the hotkey. Handy for Hammerspoon, Shortcuts or a Stream Deck button.
cd "${0:A:h}"
kill -USR1 $(cat .lock) 2>/dev/null || echo "Companion isn't running. Start it with ./start.sh"
