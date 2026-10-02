#!/bin/zsh
# Run the companion in this terminal (Ctrl+C to quit). Launch from Terminal so it shares
# Terminal's Accessibility, Screen Recording and Microphone permissions.
cd "${0:A:h}"
exec .venv/bin/python companion.py
