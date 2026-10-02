#!/bin/zsh
# Install Mac: Python packages, speech models, the screen-control helpers and your own config files.
# Safe to run again. Whisper models download themselves on first use (about 2 GB).
set -e
cd "${0:A:h}"
[[ $(uname -m) == arm64 ]] || { echo "Mac needs an Apple Silicon Mac (mlx)."; exit 1; }
(( ${$(sw_vers -productVersion)%%.*} >= 14 )) || { echo "Mac needs macOS 14 or later."; exit 1; }
xcode-select -p >/dev/null 2>&1 || { echo "First install the Xcode Command Line Tools: xcode-select --install"; exit 1; }
path=(/opt/homebrew/bin /usr/local/bin $path)
command -v brew >/dev/null || { echo "First install Homebrew: https://brew.sh"; exit 1; }
command -v cliclick >/dev/null || brew install cliclick
PY=$(command -v python3.13 || command -v python3.12) || { echo "Install Python 3.12 or 3.13: brew install python@3.13"; exit 1; }

echo "Python packages..."
[[ -x .venv/bin/python ]] || $PY -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "Speech models..."
get() {  # file, url, sha256
  [[ -f models/$1 ]] && return
  mkdir -p models && curl -fL --progress-bar -o models/$1.part $2
  [[ $(shasum -a 256 models/$1.part | cut -d' ' -f1) == $3 ]] || { echo "models/$1: wrong checksum"; exit 1; }
  mv models/$1.part models/$1
}
get silero_vad.onnx https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx \
  9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6
get wespeaker_en_voxceleb_resnet34_LM.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34_LM.onnx \
  e9848563da86f263117134dfd7ad63c92355b37de492b55e325400c9d9c39012

echo "Screen-control helpers..."
mkdir -p hands/bin
for t in screens scrollwheel; do swiftc -O hands/$t.swift -o hands/bin/$t; done

for f in config.json persona.md memory.md; do [[ -e $f ]] || cp ${f/./.example.} $f; done
[[ -e .env ]] || { cp .env.example .env && chmod 600 .env; }  # optional TypeSafe key

echo
if command -v claude >/dev/null || [[ -x ~/.local/bin/claude ]]; then
  echo "Claude Code is installed. If you haven't yet, run claude once and log in."
else
  echo "Mac talks to Claude through Claude Code. Install it and log in once:"
  echo "  curl -fsSL https://claude.ai/install.sh | bash && claude"
fi
echo "Done. Start Mac with ./start.sh, or run it as an app: ./build_app.sh && ./install_login.sh"
