"""e2e.py — end-to-end run of the BUILT Wavetype.app on a GitHub macOS runner.

Nobody on the team has a Mac: this script is the pair of eyes. It launches the binary inside
dist/Wavetype.app straight from the job's shell (not `open`: launched this way the app inherits the
runner agent's TCC grants for Accessibility, events and screen capture), feeds it a spoken WAV
through the WAVETYPE_TEST_WAV hook, presses the hotkeys with real CGEvents, takes screenshots and
reads back what landed in TextEdit. Everything goes to $RESULTS (default: results/).

    python e2e.py tests      # platform-neutral test scripts -> results/tests/
    python e2e.py e2e        # dictation scenarios + first-run screenshots -> results/e2e.json
    python e2e.py summary    # results/summary.json (stdlib only: runs even if the rest crashed)

Event contract (see the app): WAVETYPE_TEST_EVENTS=<jsonl>, one {"t":..,"ev":..} per line.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
RES = os.path.abspath(os.environ.get("RESULTS") or os.path.join(ROOT, "results"))
APP = os.path.join(ROOT, "dist", "Wavetype.app")
APP_BIN = os.path.join(APP, "Contents", "MacOS", "Wavetype")
WAV = os.environ.get("TEST_WAV") or os.path.join(RES, "test.wav")
SENTENCE = "Hello Sarah, the meeting moved to Thursday at three. Can you bring the slides?"
TESTS = ["test_mac_layer", "test_context", "test_live_engine", "test_live_dual", "test_live_local"]
READY_TIMEOUT = 600          # the first launch may download the local model
PASTE_TIMEOUT = 240
RECORD_SECS = 8
MATCH_MIN = 0.6

sys.path.insert(0, HERE)
try:
    import keys                         # needs PyObjC Quartz (in the build venv)
    KEYS_ERR = None
except Exception as e:                  # recorded in the results, the run goes on
    keys = None
    KEYS_ERR = f"{type(e).__name__}: {e}"


def now():
    return time.strftime("%H:%M:%S")


def say(msg):
    print(f"[{now()}] {msg}", flush=True)


def run(cmd, timeout=60):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def osa(script, timeout=30):
    return run(["osascript", "-e", script], timeout)


def dump(name, data):
    with open(os.path.join(RES, name), "w", encoding="utf-8") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f, indent=2, ensure_ascii=False)


# ---------- screenshots ----------
SHOTS = os.path.join(RES, "shots")


def shot(name):
    """Full-screen capture, shrunk to 1600 px and saved as JPEG (small enough for git)."""
    os.makedirs(SHOTS, exist_ok=True)
    tmp = os.path.join(SHOTS, f".{name}.png")
    out = os.path.join(SHOTS, f"{name}.jpg")
    rc, msg = run(["screencapture", "-x", tmp], 20)
    if rc != 0 or not os.path.exists(tmp):
        say(f"screencapture {name} failed: {msg.strip()}")
        with open(os.path.join(RES, "shot_errors.txt"), "a", encoding="utf-8") as f:   # run 1: 0 shots, no trace
            f.write(f"{name} rc={rc} {msg.strip()[:300]}\n")
        return None
    rc2, msg2 = run(["sips", "-Z", "1600", "-s", "format", "jpeg", "-s", "formatOptions", "80", tmp, "--out", out], 30)
    if not os.path.exists(out):
        with open(os.path.join(RES, "shot_errors.txt"), "a", encoding="utf-8") as f:
            f.write(f"{name} sips rc={rc2} {msg2.strip()[:300]}\n")
    try:
        os.remove(tmp)
    except OSError:
        pass
    return out if os.path.exists(out) else None


class Shooter(threading.Thread):
    """A screenshot every second for the first FAST shots of a phase, then one every SLOW seconds:
    a paste that never comes must not put 240 JPEGs per scenario into the public results branch."""
    FAST, SLOW = 15, 10.0

    def __init__(self, prefix):
        super().__init__(daemon=True)
        self.prefix, self.n, self.stop_flag = prefix, 0, threading.Event()

    def run(self):
        while not self.stop_flag.is_set():
            t = time.time()
            self.n += 1
            shot(f"{self.prefix}_{self.n:02d}")
            gap = 1.0 if self.n < self.FAST else self.SLOW
            self.stop_flag.wait(max(0.0, gap - (time.time() - t)))

    def stop(self):
        self.stop_flag.set()
        self.join(10)


# ---------- events from the app ----------
class Events:
    def __init__(self, path):
        self.path, self.pos, self.items = path, 0, []

    def poll(self):
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                chunk = f.read()
        except FileNotFoundError:
            return
        # only whole lines: the app may be mid-write
        cut = chunk.rfind(b"\n") + 1
        self.pos += cut
        for line in chunk[:cut].decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self.items.append(json.loads(line))
            except ValueError:
                self.items.append({"ev": "_unparsed", "raw": line[:300]})

    def since(self, i, *names):
        self.poll()
        return [e for e in self.items[i:] if not names or e.get("ev") in names]

    def wait(self, names, timeout, proc=None, start=0):
        end = time.time() + timeout
        while time.time() < end:
            hit = self.since(start, *names)
            if hit:
                return hit[0]
            if proc is not None and proc.poll() is not None:
                self.poll()
                hit = self.since(start, *names)
                return hit[0] if hit else None
            time.sleep(0.25)
        return None


# ---------- TextEdit ----------
# Driven only through System Events (UI scripting) and `open -a`. On the hosted runner the TCC
# database grants Apple Events from osascript to System Events, Finder, Safari and Terminal, not to
# TextEdit (runner-images configure-tccdb-macos.sh): `tell application "TextEdit"` would raise a
# consent dialog that nobody clicks and that takes the focus away from the dictation.
TE = 'process "TextEdit"'
TE_AREA = f'text area 1 of scroll area 1 of window 1 of {TE}'
TE_AUTOSAVE = os.path.expanduser("~/Library/Containers/com.apple.TextEdit/Data/Library/Autosave Information")


def textedit_setup():
    # plain text, and a new untitled window at launch instead of the iCloud open panel
    run(["defaults", "write", "com.apple.TextEdit", "RichText", "-int", "0"])
    run(["defaults", "write", "com.apple.TextEdit", "NSShowAppCentricOpenPanelInsteadOfUntitledFile",
         "-bool", "false"])
    # no window restoration between scenarios: each one starts from one empty document
    run(["defaults", "write", "com.apple.TextEdit", "NSQuitAlwaysKeepsWindows", "-bool", "false"])
    run(["defaults", "write", "com.apple.TextEdit", "ApplePersistenceIgnoreState", "-bool", "true"])
    # "Press fn key to: Do Nothing" (what the README tells users): the synthetic Fn of scenario 2
    # must not also open the emoji picker or system dictation on top of TextEdit
    run(["defaults", "write", "com.apple.HIToolbox", "AppleFnUsageType", "-int", "0"])
    run(["killall", "cfprefsd"])                 # the running session rereads the preferences


def textedit_quit():
    run(["killall", "TextEdit"])
    for _ in range(20):
        if run(["pgrep", "-x", "TextEdit"])[0] != 0:
            break
        time.sleep(0.25)
    shutil.rmtree(TE_AUTOSAVE, ignore_errors=True)


def textedit_new(initial=""):
    """A fresh TextEdit with one empty plain-text document in front, holding `initial`, caret at the end."""
    textedit_quit()
    rc, out = run(["open", "-a", "TextEdit"], 30)
    if rc != 0:
        return False, out.strip()
    esc = initial.replace("\\", "\\\\").replace('"', '\\"')
    rc, out = osa(f'''
tell application "System Events"
    repeat 40 times
        if exists {TE} then
            if (count windows of {TE}) > 0 then exit repeat
        end if
        delay 0.25
    end repeat
    set frontmost of {TE} to true
    delay 0.3
    keystroke "n" using command down
    delay 0.7
    set value of {TE_AREA} to "{esc}"
    delay 0.3
    key code 125 using command down
end tell
''')
    return rc == 0, out.strip()


def textedit_focus():
    run(["open", "-a", "TextEdit"], 30)          # LaunchServices: brings it to the front, no Apple Events
    osa(f'tell application "System Events" to set frontmost of {TE} to true')
    time.sleep(0.7)


def textedit_text():
    rc, out = osa(f'tell application "System Events" to get value of {TE_AREA}')
    return out.rstrip("\n") if rc == 0 else None


def frontmost():
    rc, out = osa('tell application "System Events" to get name of first process whose frontmost is true')
    return out.strip() if rc == 0 else None


# ---------- the app ----------
def launch(tag, extra):
    ev_path = os.path.join(RES, f"events_{tag}.jsonl")
    if os.path.exists(ev_path):
        os.remove(ev_path)
    env = dict(os.environ)
    for k in ("WAVETYPE_TEST_WAV", "WAVETYPE_SKIP_FIRST_RUN", "WAVETYPE_FIRST_RUN_PAGE"):
        env.pop(k, None)
    env["WAVETYPE_TEST_EVENTS"] = ev_path
    env.update(extra)
    out = open(os.path.join(RES, "logs", f"app_{tag}.out"), "w", encoding="utf-8")
    say(f"launch {tag}: {APP_BIN} {extra}")
    p = subprocess.Popen([APP_BIN], env=env, stdout=out, stderr=subprocess.STDOUT, cwd=os.path.expanduser("~"))
    p._wt_out = out
    return p, Events(ev_path)


def stop(p, use_hotkey=True):
    """Ctrl+Option+Q first (the real way out), then SIGTERM, then SIGKILL. Returns how it ended."""
    how = "already exited"
    if p.poll() is None and use_hotkey and keys:
        try:
            keys.press("quit")
        except Exception as e:
            say(f"quit hotkey failed: {e}")
        try:
            p.wait(10)
            how = "quit hotkey"
        except subprocess.TimeoutExpired:
            pass
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(8)
            how = "SIGTERM"
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)
            how = "SIGKILL"
    try:
        p._wt_out.close()
    except Exception:
        pass
    return {"how": how, "exit_code": p.returncode}


def words(s):
    return re.findall(r"[a-z0-9']+", (s or "").lower())


def match_ratio(text, sentence=SENTENCE):
    want = words(sentence)
    have = set(words(text))
    hit = [w for w in want if w in have]
    return round(len(hit) / len(want), 3) if want else 0.0


def dictation(tag, key, initial=""):
    """One dictation: start with `key`, ~8 s of the WAV, stop with `key`, read TextEdit."""
    r = {"tag": tag, "key": key, "initial_text": initial}
    if not os.path.exists(APP_BIN):
        r["error"] = "app not built"
        return r
    ok, msg = textedit_new(initial)
    r["textedit_ready"] = ok
    if not ok:
        r["textedit_error"] = msg[:500]
    p, ev = launch(tag, {"WAVETYPE_TEST_WAV": WAV, "WAVETYPE_SKIP_FIRST_RUN": "1"})
    try:
        time.sleep(3)
        r["launched"] = p.poll() is None or bool(ev.since(0))
        t0 = time.time()
        # "ready" = the app reached its main loop (it says so with ready, or with tap_failed when
        # the key tap could not be created); "tap_ok" = the tap is live (a ready event arrived).
        first = ev.wait(("ready", "tap_failed"), READY_TIMEOUT, p)
        r["ready_after_s"] = round(time.time() - t0, 1)
        r["ready"] = bool(first)
        if first and first.get("ev") == "tap_failed":
            ev.wait(("ready",), 30, p)              # the app retries the tap every 2 s
        r["tap_failed"] = ev.since(0, "tap_failed")[:5]
        r["tap_ok"] = bool(ev.since(0, "ready"))
        r["alive_at_ready"] = p.poll() is None
        shot(f"{tag}_0_ready")
        if not r["tap_ok"] or p.poll() is not None:
            r["error"] = ("exited with %s" % p.returncode if p.poll() is not None
                          else "no ready event" if not first else "key tap never became active")
            return r

        textedit_focus()
        r["frontmost_before"] = frontmost()
        i0 = len(ev.items)
        keys.press(key)
        rs = ev.wait(("rec_start",), 10, p, start=i0)
        r["rec_start"] = bool(rs)
        r["hotkey_events"] = ev.since(i0, "hotkey")
        if not rs:
            shot(f"{tag}_1_no_rec_start")
            r["error"] = "no rec_start after the hotkey"
            return r
        shooter = Shooter(f"{tag}_1_rec")
        shooter.start()
        time.sleep(RECORD_SECS)
        keys.press(key)
        r["rec_stop"] = bool(ev.wait(("rec_stop",), 10, p, start=i0))
        time.sleep(2)
        shooter.stop()
        proc = Shooter(f"{tag}_2_processing")
        proc.start()
        pasted = ev.wait(("pasted",), PASTE_TIMEOUT, p, start=i0)
        proc.stop()
        time.sleep(1.5)
        shot(f"{tag}_3_after_paste")
        text = textedit_text()
        with open(os.path.join(RES, f"textedit_{tag}.txt"), "w", encoding="utf-8") as f:
            f.write(text if text is not None else "<could not read TextEdit>")
        r["textedit_text"] = text
        r["frontmost_after"] = frontmost()
        r["hotkey_events"] = ev.since(i0, "hotkey")
        r["card_events"] = len(ev.since(i0, "card"))
        r["card_shown"] = r["card_events"] > 0
        r["card_phases"] = sorted({str(e.get("phase")) for e in ev.since(i0, "card")})
        r["caret"] = ev.since(i0, "caret")[:3]
        r["context"] = ev.since(i0, "context")[:3]
        tr = ev.since(i0, "transcript")
        r["transcript_events"] = tr
        r["transcript"] = any((e.get("chars") or 0) > 0 for e in tr)
        r["pasted"] = pasted
        body = text or ""
        if initial and body.lower().startswith(initial.strip().lower()):
            body = body[len(initial.strip()):]
        r["word_ratio"] = match_ratio(body)
        r["pasted_text_matches"] = r["word_ratio"] >= MATCH_MIN
        if initial:
            r["initial_text_kept"] = (text or "").lower().startswith(initial.strip().lower())
        r["errors"] = ev.since(0, "error")
        return r
    except Exception:
        r["exception"] = traceback.format_exc()
        return r
    finally:
        r["stop"] = stop(p)
        r["events_total"] = len(ev.since(0))


def first_run_page(page):
    r = {"page": page}
    if not os.path.exists(APP_BIN):
        r["error"] = "app not built"
        return r
    p, ev = launch(f"first_run_{page}", {"WAVETYPE_FIRST_RUN_PAGE": page})
    try:
        time.sleep(10)
        r["alive_after_10s"] = p.poll() is None
        r["screenshot"] = shot(f"first_run_{page}")
        time.sleep(3)
        r["screenshot_2"] = shot(f"first_run_{page}_b")
    finally:
        r["stop"] = stop(p, use_hotkey=False)
        r["events"] = ev.since(0)[:20]
    return r


def collect_logs():
    """wavetype.log / crash.log wherever the Mac paths put them."""
    home = os.path.expanduser("~")
    seen = []
    for base in ("Library/Application Support/Wavetype", "Library/Caches/Wavetype", "Library/Logs/Wavetype"):
        for name in ("wavetype.log", "crash.log"):
            for src in glob.glob(os.path.join(home, base, "**", name), recursive=True):
                dst = os.path.join(RES, "logs", src.replace(home + "/", "").replace("/", "__"))
                shutil.copyfile(src, dst)
                seen.append(src.replace(home, "~"))
    return seen


def app_facts():
    facts = {"app_exists": os.path.exists(APP_BIN)}
    if not facts["app_exists"]:
        return facts
    plist = os.path.join(APP, "Contents", "Info.plist")
    facts["info_plist"] = run(["plutil", "-p", plist])[1]
    facts["codesign"] = run(["codesign", "-dv", "--verbose=4", APP])[1]
    facts["entitlements"] = run(["codesign", "-d", "--entitlements", "-", "--xml", APP])[1][:3000]
    facts["codesign_verify"] = run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", APP], 300)
    facts["spctl"] = run(["spctl", "-a", "-vv", APP])  # ad-hoc: rejected, expected
    facts["size"] = run(["du", "-sh", APP])[1].strip()
    facts["dmg"] = [os.path.basename(d) for d in glob.glob(os.path.join(ROOT, "dist", "*.dmg"))]
    return facts


def cmd_e2e():
    for d in ("logs", "shots"):
        os.makedirs(os.path.join(RES, d), exist_ok=True)
    out = {"started": time.time(), "wav": WAV, "wav_exists": os.path.exists(WAV), "keys_error": KEYS_ERR}
    try:
        out["app"] = app_facts()
        out["driver_preflight"] = keys.preflight() if keys else None
        if keys is None:
            out["error"] = f"cannot post hotkeys: {KEYS_ERR}"
        else:
            textedit_setup()
            out["scenarios"] = {}
            for tag, key, initial in (("s1_ctrl_option", "ctrl-option", ""),
                                      ("s2_fn", "fn", ""),
                                      ("s3_context", "ctrl-option", "I think ")):
                say(f"scenario {tag}")
                out["scenarios"][tag] = dictation(tag, key, initial)
                dump("e2e.json", out)               # partial results survive a crash
            textedit_quit()
        out["first_run"] = {}
        for page in ("permissions", "key"):
            say(f"first run page {page}")
            out["first_run"][page] = first_run_page(page)
    except Exception:
        out["exception"] = traceback.format_exc()
    finally:
        out["logs_found"] = collect_logs()
        out["ended"] = time.time()
        dump("e2e.json", out)
    say("e2e done")


def cmd_tests():
    os.makedirs(os.path.join(RES, "tests"), exist_ok=True)
    venv = os.path.join(ROOT, "build", "mac", "venv", "bin", "python")
    py = venv if os.path.exists(venv) else sys.executable
    for t in TESTS:
        path = os.path.join(ROOT, "tests", f"{t}.py")
        log = os.path.join(RES, "tests", f"{t}.txt")
        if not os.path.exists(path):
            with open(log, "w") as f:
                f.write("missing\n")
            rc = 127
        else:
            say(f"test {t}")
            try:
                with open(log, "w", encoding="utf-8") as f:
                    rc = subprocess.run([py, path], cwd=ROOT, stdout=f, stderr=subprocess.STDOUT,
                                        timeout=900).returncode
            except subprocess.TimeoutExpired:
                rc = -9
                with open(log, "a") as f:
                    f.write("\nTIMEOUT after 900 s\n")
        with open(os.path.join(RES, "tests", f"{t}.exit"), "w") as f:
            f.write(str(rc))


def _test_result(t):
    log = os.path.join(RES, "tests", f"{t}.txt")
    try:
        text = open(log, encoding="utf-8", errors="replace").read()
        rc = int(open(os.path.join(RES, "tests", f"{t}.exit")).read().strip())
    except Exception:
        return {"ok": False, "line": "not run", "exit": None}
    hits = re.findall(r"(\d+)\s*/\s*(\d+)\s+(?:ok|passati)", text)   # live_engine/dual say "passati"
    if not hits:
        return {"ok": False, "line": text.strip().splitlines()[-1][:200] if text.strip() else "", "exit": rc}
    a, b = hits[-1]
    return {"ok": a == b and rc == 0, "line": f"{a}/{b} ok", "exit": rc}


def cmd_summary():
    try:
        e2e = json.load(open(os.path.join(RES, "e2e.json"), encoding="utf-8"))
    except Exception as e:
        e2e = {"error": f"no e2e.json: {e}"}
    sc = e2e.get("scenarios") or {}
    fr = e2e.get("first_run") or {}

    def every(key):
        vals = {k: bool(v.get(key)) for k, v in sc.items()}
        return {"pass": bool(vals) and all(vals.values()), "by_scenario": vals}

    checks = {
        "built": {"pass": os.path.exists(APP_BIN), "detail": "dist/Wavetype.app/Contents/MacOS/Wavetype"},
        "dmg": {"pass": bool(glob.glob(os.path.join(ROOT, "dist", "*.dmg")))},
    }
    for k in ("launched", "ready", "tap_ok", "rec_start", "card_shown", "transcript", "pasted_text_matches"):
        checks[k] = every(k)
    checks["pasted_text_matches"]["word_ratio"] = {k: v.get("word_ratio") for k, v in sc.items()}
    checks["pasted_text_matches"]["min"] = MATCH_MIN
    def first_run_ok(r):
        r = r or {}
        errors = [e for e in (r.get("events") or []) if e.get("ev") == "error"]
        return bool(r.get("screenshot")) and bool(r.get("alive_after_10s")) and not errors

    shots = {p: first_run_ok(fr.get(p)) for p in ("permissions", "key")}
    checks["first_run_screenshots"] = {"pass": all(shots.values()), "by_page": shots,
                                       "rule": "screenshot taken, app alive after 10 s, no error event"}
    tests = {t: _test_result(t) for t in TESTS}
    summary = {
        "run": {k: os.environ.get(k) for k in ("GITHUB_RUN_NUMBER", "GITHUB_RUN_ID", "GITHUB_SHA",
                                                "GITHUB_REF_NAME", "RUNNER_OS", "RUNNER_ARCH")},
        "all_ok": all(c["pass"] for c in checks.values()),
        "checks": checks,
        "tests_ok": all(t["ok"] for t in tests.values()),
        "tests": tests,
        "scenarios_brief": {k: {x: v.get(x) for x in ("ready_after_s", "frontmost_before", "textedit_text",
                                                       "word_ratio", "error", "stop", "initial_text_kept")}
                            for k, v in sc.items()},
        "driver_preflight": e2e.get("driver_preflight"),
        "e2e_error": e2e.get("error") or (e2e.get("exception") or "")[-1500:] or None,
        "logs_found": e2e.get("logs_found"),
    }
    dump("summary.json", summary)
    print(json.dumps({k: v["pass"] for k, v in checks.items()}, indent=1))
    print("tests:", {t: v["line"] for t, v in tests.items()})
    print("all_ok:", summary["all_ok"])


if __name__ == "__main__":
    os.makedirs(RES, exist_ok=True)
    {"e2e": cmd_e2e, "tests": cmd_tests, "summary": cmd_summary}[sys.argv[1] if len(sys.argv) > 1 else "summary"]()
