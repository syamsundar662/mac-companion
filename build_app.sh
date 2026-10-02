#!/bin/zsh
# Build ~/Applications/Mac.app: a small launcher that runs this project's companion.py (see README, "Mac.app").
set -e
cd "${0:A:h}"
APP=~/Applications/Mac.app
TMP=$(mktemp -d) && trap 'rm -rf "$TMP"' EXIT
rm -rf "$APP" && mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -O app/launcher.swift -o "$APP/Contents/MacOS/Mac"
cp app/Info.plist "$APP/Contents/"
print -r -- "$PWD" > "$APP/Contents/Resources/project_path"  # the launcher runs companion.py from here
.venv/bin/python app/make_icon.py "$TMP/AppIcon.iconset" && iconutil -c icns "$TMP/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"
codesign --force --deep --sign - "$APP"
echo "Built $APP"
