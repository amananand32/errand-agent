"""Demo UI: chat on the left, the live run trace on the right (stdlib only).

  python -m errand_agent.web --reset              # http://localhost:8765
  python -m errand_agent.web --model anthropic    # real Claude
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import PRESETS, build
from .proactive import ProactiveMonitor

PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Errand Agent demo</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--panel:#171a21;--ink:#e6e8ee;--mute:#8a93a6;--acc:#f59e0b;--ok:#22c55e;--bad:#ef4444;--line:#262b36}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 ui-sans-serif,system-ui,Segoe UI,Roboto}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line)}
header b{color:var(--acc)}header .sp{flex:1}button{background:#232837;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px 10px;cursor:pointer}
main{display:grid;grid-template-columns:1fr 1fr;height:calc(100vh - 50px)}
#chat,#trace{overflow:auto;padding:14px}#chat{border-right:1px solid var(--line)}
.m{max-width:92%;margin:8px 0;padding:10px 12px;border-radius:10px;white-space:pre-wrap}
.u{background:#1e3a8a;margin-left:auto}.a{background:var(--panel);border:1px solid var(--line)}.n{background:#3b2a0a;border:1px solid #6b4f12}
form{display:flex;gap:8px;padding:10px;border-top:1px solid var(--line);position:sticky;bottom:0;background:var(--bg)}
input{flex:1;background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:10px}
.run{border:1px solid var(--line);border-radius:8px;margin-bottom:12px}.run h4{margin:0;padding:6px 10px;background:var(--panel);font-size:12px;color:var(--mute)}
.ev{font:12px/1.4 ui-monospace,Menlo,monospace;padding:3px 10px;border-top:1px solid #1d212b;white-space:pre-wrap;word-break:break-word}
.t-tool_call{color:#93c5fd}.t-model_call{color:#c4b5fd}.t-state_transition{color:var(--acc)}.err{color:var(--bad)}
.t-booking_verified,.t-approval_granted{color:var(--ok)}.t-injection_flagged,.t-blocked_tool_call,.t-blocked_request_change,.t-blocked_memory_write,.t-unverified_claim_blocked{color:var(--bad)}
.t-context{color:var(--mute)}
@media(max-width:800px){main{grid-template-columns:1fr;height:auto}#chat{border-right:0}}
</style></head><body>
<header><b>Errand</b><span id="meta"></span><span class="sp"></span>
<button onclick="admin('tick')">Run monitor</button><button onclick="cancelProv()">Provider cancels…</button>
<button onclick="admin('advance',{minutes:11})">+11 min</button></header>
<main><section style="display:flex;flex-direction:column"><div id="chat" style="flex:1"></div>
<form onsubmit="send(event)"><input id="q" autocomplete="off" placeholder="e.g. Find a plumber for my kitchen tap this week, mornings only, under ₹1,500."><button>Send</button></form></section>
<section id="trace"></section></main>
<script>
const chat=document.getElementById('chat'),trace=document.getElementById('trace');
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function add(cls,t){const d=document.createElement('div');d.className='m '+cls;d.innerHTML=esc(t).replace(/\*\*(.+?)\*\*/g,'<b>$1</b>');chat.appendChild(d);chat.scrollTop=1e9}
function fmt(e){const k=Object.assign({},e);['seq','run_id','ts','type','tb'].forEach(x=>delete k[x]);
 if(e.type==='tool_call')return `${e.t_ms}ms  tool ${e.tool}(${JSON.stringify(e.args)})  ${e.error?'ERROR '+e.error:'-> '+JSON.stringify(e.result).slice(0,160)}  [${e.latency_ms}ms]`;
 if(e.type==='model_call')return `${e.t_ms}ms  model ${e.model} in=${e.tokens_in} out=${e.tokens_out} $${e.est_cost_usd} -> ${e.tool_uses.join(',')||'final text'}`;
 if(e.type==='state_transition')return `${e.t_ms}ms  STATE ${e.from} -> ${e.to}  (${e.reason})`;
 if(e.type==='context')return `${e.t_ms}ms  context ${e.tokens_total}/${e.budget_tokens} tok · memories: ${e.memories_included.join('; ')} · history kept ${e.history_message_ids.length}, dropped ${e.history_dropped}`;
 return `${e.t_ms}ms  ${e.type} ${JSON.stringify(k).slice(0,220)}`}
function showRun(id,evs){const r=document.createElement('div');r.className='run';r.innerHTML=`<h4>${id}</h4>`;
 evs.forEach(e=>{const d=document.createElement('div');d.className='ev t-'+e.type+(e.error?' err':'');d.textContent=fmt(e);r.appendChild(d)});trace.prepend(r)}
async function send(ev){ev.preventDefault();const q=document.getElementById('q');const t=q.value.trim();if(!t)return;q.value='';add('u',t);
 const r=await (await fetch('/api/chat',{method:'POST',body:JSON.stringify({text:t})})).json();add('a',r.reply);showRun(r.run_id+' · '+r.outcome,r.events)}
async function admin(cmd,extra){const r=await (await fetch('/api/admin',{method:'POST',body:JSON.stringify(Object.assign({cmd},extra||{}))})).json();
 (r.notes||[]).forEach(n=>add('n',n));if(r.info)add('n',r.info);if(r.events)showRun(r.run_id+' · '+cmd,r.events)}
function cancelProv(){const id=prompt('Booking id cancelled by provider (e.g. BK-2002)');if(id)admin('provider_cancel',{booking_id:id})}
fetch('/api/meta').then(r=>r.json()).then(m=>{document.getElementById('meta').textContent=m.meta;(m.notices||[]).forEach(n=>add('n',n))});
</script></body></html>"""


def serve(port: int, agent) -> None:
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/api/meta":
                return self._json({"meta": f"model={agent.model.name} · today {agent.clock.now():%a %d %b %H:%M}",
                                   "notices": agent.notices})
            b = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with lock:
                if self.path == "/api/chat":
                    r = agent.handle(body.get("text", ""))
                    return self._json({"reply": r.reply, "outcome": r.outcome, "run_id": r.run_id,
                                       "events": agent.tracer.events})
                cmd = body.get("cmd")
                if cmd == "tick":
                    notes = ProactiveMonitor(agent).tick()
                    return self._json({"notes": notes or ["(monitor: no changes)"], "run_id": agent.tracer.run_id,
                                       "events": agent.tracer.events})
                if cmd == "provider_cancel":
                    agent.tools.svc.simulate_provider_cancellation(body["booking_id"])
                    return self._json({"info": f"(simulated: provider cancelled {body['booking_id']} - press "
                                               "'Run monitor')"})
                if cmd == "advance" and hasattr(agent.clock, "advance"):
                    agent.clock.advance(minutes=float(body.get("minutes", 11)))
                    return self._json({"info": f"(clock advanced to {agent.clock.now():%a %H:%M})"})
            self._json({"error": "unknown"}, 404)

    print(f"Errand demo on http://localhost:{port}  (traces in {agent.tracer.trace_dir})")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="rule")
    ap.add_argument("--preset", default="timeout", choices=sorted(PRESETS))
    ap.add_argument("--data", default="data")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--record")
    ap.add_argument("--real-clock", action="store_true")
    a = ap.parse_args()
    serve(a.port, build(a.data, a.model, a.preset, a.reset, a.record, a.real_clock))


if __name__ == "__main__":
    main()
