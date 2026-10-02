#!/usr/bin/env python3
"""mac-computer: minimal stdio MCP server giving Claude screen+mouse+keyboard on this Mac.
Deps: cliclick (brew install cliclick), screencapture, osascript, swiftc for the helpers in bin/. No pip deps.
Env: MAC_COMPUTER_STOP kill-switch file (default ~/.mac-companion/STOP; while it exists, actions are refused),
MAC_COMPUTER_CLICLICK cliclick path, MAC_COMPUTER_MIC_GUARD optional file that also refuses actions while it exists,
MAC_COMPUTER_PAUSE seconds to wait after an action. Log: ~/Library/Logs/mac-companion/actions.log."""
import base64, json, os, shutil, subprocess, sys, time, tempfile, re

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "bin")
STOP = os.path.expanduser(os.environ.get("MAC_COMPUTER_STOP") or "~/.mac-companion/STOP")
MIC_GUARD = os.path.expanduser(os.environ.get("MAC_COMPUTER_MIC_GUARD", ""))
LOG = os.path.expanduser("~/Library/Logs/mac-companion/actions.log")
CLICLICK = os.path.expanduser(os.environ.get("MAC_COMPUTER_CLICLICK") or next(
    (c for c in ("/opt/homebrew/bin/cliclick", "/usr/local/bin/cliclick") if os.path.exists(c)), shutil.which("cliclick") or "cliclick"))
STEP_PAUSE = float(os.environ.get("MAC_COMPUTER_PAUSE", "1.0"))
MAX_W = 1600  # downscale screenshots for the model

def log(msg):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")

def sh(cmd, timeout=20):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

def guard():
    if os.path.exists(STOP):
        raise RuntimeError(f"STOP file present (kill switch). Remove {STOP} to resume.")
    if MIC_GUARD and os.path.exists(MIC_GUARD):
        raise RuntimeError("mic-guard recording active; refusing to act.")

def helper(name):
    """bin/<name>, built from <name>.swift the first time it's needed."""
    path = os.path.join(BIN, name)
    if not os.path.exists(path):
        os.makedirs(BIN, exist_ok=True)
        sh(["swiftc", "-O", os.path.join(HERE, name + ".swift"), "-o", path], timeout=300)
    return path

def displays():
    rc, out, _ = sh([helper("screens")])
    return json.loads(out) if rc == 0 else [{"id": 1, "x": 0, "y": 0, "w": 0, "h": 0, "main": True}]

def screenshot(display=1):
    d = next((x for x in displays() if x["id"] == display), displays()[0])
    fd, path = tempfile.mkstemp(suffix=".png"); os.close(fd)
    sh(["screencapture", "-x", "-D", str(display), path])
    sh(["sips", "--resampleWidth", str(MAX_W), path], timeout=30)
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    os.remove(path)
    return data, d

def geom_text(d):
    k = d["w"] / MAX_W if d.get("w") else 1
    return (f"display={d['id']} origin=({d['x']},{d['y']}) size={d['w']}x{d['h']}pt image_width={MAX_W} "
            f"=> real_x = {d['x']} + img_x*{k:.3f}, real_y = {d['y']} + img_y*{k:.3f}; frontmost={frontmost()}")

def frontmost():
    rc, out, _ = sh(["osascript", "-e", 'tell application "System Events"', "-e", "set p to first process whose frontmost is true",
                     "-e", 'set t to ""', "-e", "try", "-e", "set t to name of window 1 of p", "-e", "end try",
                     "-e", 'return (name of p) & " | " & t', "-e", "end tell"])
    return out

TOOLS = [
 {"name": "screenshot", "description": "Capture one display (1 = main; call displays for others), downscaled to 1600px wide. Returns image + the formula to convert image px to REAL global points. All other tools take REAL global points.",
  "inputSchema": {"type": "object", "properties": {"display": {"type": "integer", "default": 1}}}},
 {"name": "displays", "description": "List displays with global point origin/size/scale.",
  "inputSchema": {"type": "object", "properties": {}}},
 {"name": "click", "description": "Click at real screen point. button: left|right|double. Optional modifiers e.g. 'cmd', 'shift'.",
  "inputSchema": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string", "default": "left"}}, "required": ["x", "y"]}},
 {"name": "move", "description": "Move mouse to real screen point (hover).",
  "inputSchema": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["x", "y"]}},
 {"name": "drag", "description": "Drag from (x1,y1) to (x2,y2).",
  "inputSchema": {"type": "object", "properties": {"x1": {"type": "integer"}, "y1": {"type": "integer"}, "x2": {"type": "integer"}, "y2": {"type": "integer"}}, "required": ["x1", "y1", "x2", "y2"]}},
 {"name": "scroll", "description": "Real scroll-wheel at point (works in iPhone Mirroring too). dy in pixels: negative = scroll content down (see further), positive = back up. Default -300.",
  "inputSchema": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "dy": {"type": "integer", "default": -300}}, "required": ["x", "y"]}},
 {"name": "type", "description": "Type literal text into the focused field. ALWAYS pass expect_app (frontmost app name) so a focus change aborts instead of typing into the wrong window. Refuses messaging apps (WhatsApp/Mail/Messages/Slack...) unless allow_messaging=true.",
  "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "expect_app": {"type": "string"}, "allow_messaging": {"type": "boolean"}}, "required": ["text", "expect_app"]}},
 {"name": "key", "description": "Press a key combo, e.g. 'cmd+l', 'enter', 'esc', 'tab', 'pagedown', 'arrow-down'. ALWAYS pass expect_app. Same messaging-app refusal as type.",
  "inputSchema": {"type": "object", "properties": {"combo": {"type": "string"}, "expect_app": {"type": "string"}, "allow_messaging": {"type": "boolean"}}, "required": ["combo", "expect_app"]}},
 {"name": "open_app", "description": "Activate an app by name (e.g. 'Finder', 'Safari', 'Home Assistant'), or open a URL/file path.",
  "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}},
 {"name": "frontmost", "description": "Return frontmost app name and window title.",
  "inputSchema": {"type": "object", "properties": {}}},
]

KEYMAP = {"enter": "return", "return": "return", "esc": "esc", "escape": "esc", "tab": "tab", "space": "space",
          "delete": "delete", "backspace": "delete", "pagedown": "page-down", "pageup": "page-up", "home": "home", "end": "end",
          "up": "arrow-up", "down": "arrow-down", "left": "arrow-left", "right": "arrow-right",
          "arrow-up": "arrow-up", "arrow-down": "arrow-down", "arrow-left": "arrow-left", "arrow-right": "arrow-right"}
MODS = {"cmd": "cmd", "command": "cmd", "shift": "shift", "alt": "alt", "option": "alt", "ctrl": "ctrl", "control": "ctrl", "fn": "fn"}

def cliclick(*args):
    rc, out, err = sh([CLICLICK, *args])
    if rc != 0:
        raise RuntimeError(f"cliclick failed: {err or out}")
    return out

def do_key(combo):
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    mods = [MODS[p] for p in parts if p in MODS]
    keys = [p for p in parts if p not in MODS]
    if not keys:
        raise RuntimeError("no key in combo")
    k = keys[0]
    args = []
    if mods: args.append("kd:" + ",".join(mods))
    if k in KEYMAP: args.append("kp:" + KEYMAP[k])
    elif len(k) == 1: args.append("t:" + k)
    elif re.fullmatch(r"f\d{1,2}", k): args.append("kp:" + k)
    else: raise RuntimeError(f"unknown key {k}")
    if mods: args.append("ku:" + ",".join(mods))
    cliclick(*args)

MESSAGING = ("whatsapp", "messages", "mail", "slack", "telegram", "discord", "teams")

def focus_guard(a):
    fm = frontmost()
    app = fm.split(" | ")[0].strip().lower()
    exp = a.get("expect_app")
    if exp and exp.lower() not in app:
        raise RuntimeError(f"focus changed: frontmost is '{fm}', expected '{exp}'. Nothing typed.")
    if any(m in app for m in MESSAGING) and not a.get("allow_messaging"):
        raise RuntimeError(f"refusing to type/press keys while '{fm}' is frontmost (messaging app). Pass allow_messaging=true only when the user asked for it.")

def call(name, a):
    if name == "screenshot":
        data, d = screenshot(int(a.get("display", 1)))
        return [{"type": "text", "text": geom_text(d)}, {"type": "image", "data": data, "mimeType": "image/png"}]
    if name == "displays":
        return [{"type": "text", "text": json.dumps(displays())}]
    if name == "frontmost":
        return [{"type": "text", "text": frontmost()}]
    guard()
    if name == "click":
        b = a.get("button", "left"); x, y = a["x"], a["y"]
        cmd = {"left": "c", "right": "rc", "double": "dc"}[b]
        cliclick(f"{cmd}:={x},={y}"); log(f"click {b} {x},{y}")
    elif name == "move":
        cliclick(f"m:={a['x']},={a['y']}")
    elif name == "drag":
        cliclick(f"dd:={a['x1']},={a['y1']}", "w:200", f"dm:={a['x2']},={a['y2']}", "w:100", f"du:={a['x2']},={a['y2']}"); log(f"drag {a}")
    elif name == "scroll":
        dy = int(a.get("dy", -300))
        rc, out, err = sh([helper("scrollwheel"), str(a["x"]), str(a["y"]), str(dy)])
        if rc != 0: raise RuntimeError(err or out)
        log(f"scroll {a}")
    elif name == "type":
        focus_guard(a); cliclick("t:" + a["text"]); log(f"type {len(a['text'])} chars")
    elif name == "key":
        focus_guard(a); do_key(a["combo"]); log(f"key {a['combo']}")
    elif name == "open_app":
        t = a["target"]
        if t.startswith(("http://", "https://", "/")): sh(["open", t])
        else: sh(["open", "-a", t])
        log(f"open {t}")
    else:
        raise RuntimeError(f"unknown tool {name}")
    time.sleep(STEP_PAUSE)
    disp = 1
    for d in displays():
        px = a.get("x", a.get("x2")); py = a.get("y", a.get("y2"))
        if px is not None and d["x"] <= px < d["x"] + d["w"] and d["y"] <= py < d["y"] + d["h"]: disp = d["id"]
    data, d = screenshot(disp)
    return [{"type": "text", "text": f"ok {name}; " + geom_text(d)}, {"type": "image", "data": data, "mimeType": "image/png"}]

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try: req = json.loads(line)
    except Exception: continue
    mid = req.get("id"); m = req.get("method")
    if m == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "mac-computer", "version": "0.1"}}})
    elif m == "notifications/initialized": pass
    elif m == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif m == "tools/call":
        p = req.get("params", {})
        try:
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": call(p.get("name"), p.get("arguments") or {})}})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True}})
    elif m == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})
