"""Screen-record the side-by-side demo UI (chat | trace) in headless Chrome via the DevTools
protocol and encode docs/demo/errand_demo.mp4.

This is a real recording of the real web UI and agent (offline RuleModel, 'timeout' preset),
not a mock-up. For the submission Loom with a live Claude model, follow docs/DEMO.md.

    pip install websocket-client && python scripts/record_demo.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "demo", "errand_demo.mp4")
PORT, CDP = 8777, 9333
W, H = 1440, 900
SCALE = 1.3   # pacing: target 3-4 minutes
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Rec:
    def __init__(self, ws, frames_dir):
        self.ws, self.dir, self.n, self.i, self.items = ws, frames_dir, 0, 0, []

    def cmd(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})

    def js(self, expr):
        r = self.cmd("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True)
        return r.get("result", {}).get("value")

    def shot(self, dur):
        dur *= SCALE
        data = self.cmd("Page.captureScreenshot", format="png")["data"]
        self.n += 1
        p = os.path.join(self.dir, f"f{self.n:05d}.png")
        with open(p, "wb") as f:
            f.write(base64.b64decode(data))
        self.items.append((p, dur))

    def caption(self, text, hold=0.0):
        self.js("(()=>{let d=document.getElementById('cap');if(!d){d=document.createElement('div');d.id='cap';"
                "d.style.cssText='position:fixed;left:50%;bottom:18px;transform:translateX(-50%);max-width:1100px;"
                "background:#fde68a;color:#111;font:600 17px system-ui;padding:10px 16px;border-radius:10px;"
                "box-shadow:0 6px 24px #0008;z-index:9';document.body.appendChild(d)}"
                f"d.textContent={json.dumps(text)};d.style.display={json.dumps('block' if text else 'none')}}})()")
        if hold:
            self.shot(hold)

    def type_and_send(self, text, hold_after=6.0):
        for k in range(0, len(text) + 1, 3):
            self.js(f"document.getElementById('q').value={json.dumps(text[:k])}")
            self.shot(0.07)
        self.js(f"document.getElementById('q').value={json.dumps(text)}")
        self.shot(0.6)
        self.js("send({preventDefault(){}})")
        self.js("document.getElementById('chat').scrollTop=1e9")
        self.shot(hold_after)

    def scroll_trace(self, steps=4, dur=2.5):
        for s in range(steps):
            self.js(f"document.getElementById('trace').scrollTop={s * 260}")
            self.shot(dur)
        self.js("document.getElementById('trace').scrollTop=0")


def main():
    data = tempfile.mkdtemp(prefix="errand_demo_")
    frames = tempfile.mkdtemp(prefix="errand_frames_")
    env = dict(os.environ, PYTHONPATH=ROOT)
    srv = subprocess.Popen([sys.executable, "-m", "errand_agent.web", "--data", data, "--reset", "--port", str(PORT)],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    chrome = subprocess.Popen(["google-chrome", "--headless=new", f"--remote-debugging-port={CDP}", "--no-sandbox",
                               "--disable-gpu", f"--window-size={W},{H}", f"--user-data-dir={tempfile.mkdtemp()}",
                               "--no-proxy-server", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                targets = json.load(opener.open(f"http://127.0.0.1:{CDP}/json"))
                page = next(t for t in targets if t["type"] == "page")
                opener.open(f"http://127.0.0.1:{PORT}/api/meta")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True)
        r = Rec(ws, frames)
        r.cmd("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=1, mobile=False)
        r.cmd("Page.navigate", url=f"http://127.0.0.1:{PORT}/")
        time.sleep(1.5)

        r.caption("Errand agent demo - chat on the left, the live run trace on the right. Offline scripted model, "
                  "mock providers ('timeout' preset). 52 seeded memories + 322-message prior conversation.", 7)
        r.caption("1) A request with service, window and budget. Area comes from saved memory - no question needed.")
        r.type_and_send("Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.", 7)
        r.caption("Trace: context manifest (tokens, which memories were sent), model calls with tokens/cost, tool calls.", 0)
        r.scroll_trace(4, 3.5)
        r.caption("A review of the top-rated plumber contains a prompt injection. It is flagged, the provider is skipped,"
                  " and the user is warned.", 7)
        r.caption("The confirmation card is rendered by the app from the stored proposal (provider, slot, quoted price).", 7)

        r.caption("2) User approves. The first book() call is configured to TIME OUT after actually booking.")
        r.type_and_send("yes", 6)
        r.caption("Ledger written in_flight BEFORE book(); book() times out after 8.6 s; state -> VERIFYING; "
                  "get_booking_status(idempotency_key) says confirmed. No blind retry.", 0)
        r.scroll_trace(3, 4.5)
        r.caption("Success is only reported once verified - and exactly one booking exists.", 6)

        r.caption("3) Repeated confirmation: the same 'yes' again.")
        r.type_and_send("yes", 4)
        r.caption("Recognised as already done - no second book() call, no duplicate.", 5)

        r.caption("4) Proactive follow-up: the provider cancels BK-2002 (simulated event).", 2)
        r.js("admin('provider_cancel',{booking_id:'BK-2002'})")
        r.shot(3)
        r.caption("The monitor polls list_bookings without any user prompt, decides to notify (visit < 24h), and finds a "
                  "replacement inside saved preferences.", 0)
        r.js("admin('tick')")
        r.js("document.getElementById('chat').scrollTop=1e9")
        r.shot(8)
        r.scroll_trace(2, 4)
        r.caption("Nothing is rebooked until the user gives fresh approval.", 4)
        r.type_and_send("yes", 7)

        r.caption("5) Memory is inspectable. Inferred facts are labelled and never sent to the model until confirmed.")
        r.type_and_send("What do you remember about me?", 9)
        r.js("document.getElementById('chat').scrollTop=1e9")
        r.shot(6)
        r.caption("6) Forgetting removes the fact and scrubs it from stored history.")
        r.type_and_send("Please forget about my dog", 6)

        r.caption("7) Corrections keep the rest of the request: change only the day.")
        r.type_and_send("Find an electrician on Thursday, budget 1200", 6)
        r.type_and_send("Actually make it Friday", 8)
        r.caption("Old proposal invalidated (material change), service/area/budget kept, new card needs a new 'yes'.", 6)
        r.type_and_send("no", 5)
        r.caption("Every exit path ends with a clear outcome. 34 automated scenarios assert these invariants: "
                  "see reports/eval_table.md.", 8)
        r.caption("", 0)
        r.shot(2)
        ws.close()
    finally:
        srv.terminate()
        chrome.terminate()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    lst = os.path.join(frames, "list.txt")
    with open(lst, "w") as f:
        for p, d in r.items:
            f.write(f"file '{p}'\nduration {d:.3f}\n")
        f.write(f"file '{r.items[-1][0]}'\n")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst, "-vf",
                    "fps=12,format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", OUT], check=True)
    total = sum(d for _, d in r.items)  # already scaled
    print(f"wrote {OUT} ({total:.0f}s, {len(r.items)} frames)")
    shutil.rmtree(frames, ignore_errors=True)


if __name__ == "__main__":
    main()
