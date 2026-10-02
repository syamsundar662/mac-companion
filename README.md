# Mac, a voice companion for your Mac

Mac lives in the menu bar. You talk to it, it talks back, and it uses your Mac for you (apps, screen, mouse,
keyboard) with Claude as its brain. Speech recognition, the wake word and the voice lock run on your Mac.

## Features
- **Voice in and out.** Local Whisper speech recognition (mlx) and spoken replies with the macOS voices. Replies are
  short and spoken while they're written. After a reply it keeps listening a few seconds, so you can just answer.
- **Wake word.** Say "Hey Mac, open Spotify and play some lofi." The name follows `name` in the config.
- **Hold ⌥ Option** to talk, let go to send. Or press **⌃⌥Space**, or type in the panel.
- **Click by name.** Mac presses buttons, fills fields and picks menu items through Accessibility, and falls back to
  screenshots and clicks when an app doesn't expose them.
- **Jev fast path and safety** (optional, needs a [TypeSafe](https://docs.typesafe.ai) key). Simple commands (open an
  app, play/pause, volume, dark mode, the time, battery) happen in well under a second without Claude. Mac goes ahead
  with everything except deleting or overwriting: before a click, key press or command it describes the action in
  words and asks you first if it deletes or overwrites. Without a key, local rules decide and everything goes to Claude.
- **Voice lock.** Menu bar > **Set up my voice…**: read 4 sentences and Mac ignores other voices (and the TV).
- **Stop any time.** Say "stop", press ⌘. or the Stop button, or just grab the mouse while Mac is using the screen.
  Kill switch: `touch ~/.mac-companion/STOP` (bind it to a hotkey if you like); delete the file to resume.
- Screen-edge glow while it listens or works, Tamil and Malayalam support, a memory it adds to itself.

## Requirements
- An Apple Silicon Mac with macOS 14 or later, the Xcode Command Line Tools (`xcode-select --install`), Homebrew,
  and Python 3.12 or 3.13 (`brew install python@3.13`).
- [Claude Code](https://claude.com/claude-code), logged in: Mac talks to Claude through it (the Claude Agent SDK).
  Install with `curl -fsSL https://claude.ai/install.sh | bash`, then run `claude` once and log in.
- About 3 GB of disk and 2 GB of memory for the speech models.

## Install
```
git clone <this repo> companion && cd companion
./install.sh                          # cliclick, .venv, models, helpers, your config files
./start.sh                            # run in the background (./stop.sh stops it, ./run.sh shows errors)
```
Or run it as a real app that starts at login and restarts after a crash:
```
./build_app.sh && ./install_login.sh  # ~/Applications/Mac.app plus a login item
```
The first start downloads the Whisper models (about 2 GB).

## Permissions
In System Settings > Privacy & Security, allow **Microphone**, **Accessibility** and **Screen & System Audio
Recording** for **Mac** (Mac.app) or for your terminal app (`./start.sh`). macOS also asks once per app Mac controls
with AppleScript. Rebuilding Mac.app changes its signature, so macOS may ask again.

## Configuration
Your settings live in files the first start (or `install.sh`) copies from the examples; they're never committed:
- `config.json` (from `config.example.json`). Restart after editing (`./stop.sh && ./start.sh`, or
  `launchctl kickstart -k gui/$(id -u)/local.mac-companion` for Mac.app).
  - `name` (also the wake word), `user_name` (empty: "the user"), `hotkey`, `push_to_talk` (`option`,
    `left_option`, `right_option` or `""`).
  - `model` and `chat_model` (Claude models for tasks and for plain questions), `effort`.
  - `speak_replies`, `voice` (any name from `say -v '?'`), `voice_rate`, `voices` (per language).
  - `language` (`auto` or a code), `languages` (e.g. `["en", "ta", "ml"]`), `vocabulary` (names the recogniser
    should expect), `whisper_model`, `wake_model`, `preview_model`, `mic` (part of a mic's name).
  - `wake_word`, `wake_aliases` (other spellings Whisper writes for the name), `follow_up`.
  - `fast_path`, `jev_safety`, `ax_tools`, `voice_lock`, `voice_match` (0 to 1, higher is stricter; `companion.log`
    shows every score), `voice_adapt`.
  - Speed: `stream_speech`, `early_final`, `early_jev`, `one_pass_stt`, `keep_warm`, `natural`, `step_pause`,
    `fresh_after_min`. Looks: `glow`, `ptt_sounds`.
  - `kill_switch_file` (default `~/.mac-companion/STOP`), `cliclick` (path, if it's not from Homebrew).
- `persona.md`: who Mac is and how it talks. `memory.md`: what it remembers about you.
- `.env` (from `.env.example`): the optional `TYPESAFE_API_KEY`.

If it wakes by mistake or misses you, run `COMPANION_DEBUG=1 ./run.sh`: `companion.log` then lists everything the
wake listener heard (including your speech, so turn it off afterwards).

## Privacy
- **Stays on your Mac:** the microphone audio, speech recognition, the wake word, the voiceprint (`voiceprint.npy`,
  256 numbers, never a recording), `memory.md`, your config and the logs (`companion.log`,
  `~/Library/Logs/mac-companion/actions.log`).
- **Goes to Anthropic** (through Claude Code): what you ask, as text, plus the persona, memory, screenshots, and what
  Mac reads from the screen while it works on your request.
- **Goes to TypeSafe** (only with a key): the text of each request (fast path) and, for the safety check, the action
  in words, commands and up to 2000 characters of a script it would run, with secret-looking values hidden.
- Models are downloaded from GitHub (sherpa-onnx) and Hugging Face (Whisper).

## Uninstall
```
./uninstall_login.sh; ./stop.sh
rm -rf ~/Applications/Mac.app ~/.mac-companion ~/Library/Logs/mac-companion
rm -rf ~/.cache/huggingface/hub/models--mlx-community--whisper-*   # the Whisper models
brew uninstall cliclick                                             # if nothing else uses it
```
Then delete this folder and remove Mac (or your terminal) from Privacy & Security.

## Files
- `companion.py`: menu bar, hotkeys, chat panel, voice flow. `brain.py`: the Claude session.
- `voice.py`: mic, wake word, Whisper, spoken replies. `voiceid.py`: the voice lock.
- `router.py`: the Jev fast path. `safety.py` and `ax.py`: what needs your yes, and what's on screen.
- `hands/`: the screen, mouse and keyboard tools (an MCP server using cliclick). `glow.py`, `ui/`: the looks.
- `app/`: the Mac.app launcher. Tests: `.venv/bin/python -m pytest -q`.

## License
MIT, see [LICENSE](LICENSE).
