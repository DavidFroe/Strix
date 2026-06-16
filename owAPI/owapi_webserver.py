# Datei: owapi_webserver.py
# Start: python owapi_webserver.py  (Port 8123, bindet an 0.0.0.0)

import os, sys, json, uuid, threading, queue, subprocess, time, re
from pathlib import Path
from flask import Flask, request, jsonify, Response, send_from_directory

# --- Konfig ---
BASE_DIR = Path(os.getcwd()).resolve()
SCRIPT = BASE_DIR / "gpt_spreadsheet_interface.py"

# --- In-Process Engine (Prio 1: kein subprocess-Overhead) ---
sys.path.insert(0, str(BASE_DIR))
try:
    from gsi.engine import run_payload as _run_payload
    from gsi.sheets import get_cached_service as _warm_service
    _warm_service()  # Google-Credentials beim Start laden
    _IN_PROCESS = True
except Exception as _e:
    _IN_PROCESS = False
    import logging as _logging
    _logging.getLogger("owapi").warning(f"In-Process-Engine nicht verfügbar, nutze subprocess: {_e}")
UPDATE_FILE = BASE_DIR / "update.json"
STATUS_FILE = BASE_DIR / "status.json"
PROJECTS_FILE = BASE_DIR / "projects.json"
PORT = 8123

app = Flask(__name__)

# --- Utilities ---
def ok(data=None, **extra):
    out = {"ok": True, "data": data}
    out.update(extra)
    return jsonify(out)

def fail(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code

def ensure_files_exist():
    if not UPDATE_FILE.exists():
        UPDATE_FILE.write_text("{}\n", encoding="utf-8")
    if not STATUS_FILE.exists():
        STATUS_FILE.write_text("{}\n", encoding="utf-8")

def run_cli(args, cwd=None, stream=False):
    """
    Startet das Backend mit unbuffered I/O und zusammengeführter stdout/stderr.
    stream=False -> (rc, stdout, "")
    stream=True  -> (proc, stdout_queue)
    """
    exe = sys.executable
    full = [exe, "-u", str(SCRIPT)] + list(args)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    if not stream:
        p = subprocess.Popen(
            full,
            cwd=cwd or BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        out = []
        for line in p.stdout:
            out.append(line)
        p.wait()
        return p.returncode, "".join(out), ""
    else:
        p = subprocess.Popen(
            full,
            cwd=cwd or BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        q = queue.Queue()

        def reader():
            for line in iter(p.stdout.readline, ""):
                q.put(line.rstrip("\n"))
            p.wait()
            q.put(None)  # Ende markieren

        threading.Thread(target=reader, daemon=True).start()
        return p, q

def parse_projects_list_from_cli_output(raw: str):
    raw = raw.strip()
    # 1) JSON-Dict => Keys als Projektnamen
    if raw.startswith("{"):
        try:
            j = json.loads(raw)
            if isinstance(j, dict):
                return [str(k) for k in j.keys()]
        except Exception:
            pass
    # 2) JSON-Array => Strings
    if raw.startswith("["):
        try:
            j = json.loads(raw)
            if isinstance(j, list):
                return [str(x) for x in j]
        except Exception:
            pass
    # 3) Fallback: Zeilen ohne Deko
    lines = [re.sub(r"^\s*[-*]\s*", "", z).strip() for z in raw.splitlines()]
    return [z for z in lines if z and z not in ("{", "}", "[", "]") and not z.endswith("{") and ":" not in z]

def parse_show_details(raw: str):
    # tolerant URL + Beschreibung extrahieren
    try:
        j = json.loads(raw)
        if isinstance(j, dict):
            return {
                "url": j.get("url") or j.get("URL") or "",
                "description": j.get("description") or j.get("desc") or "",
                "raw": raw.strip(),
            }
    except Exception:
        pass
    url = ""
    desc = ""
    m = re.search(r"(?:URL|Url)\s*[:=]\s*(\S+)", raw)
    if m:
        url = m.group(1).strip()
    m = re.search(r"(?:Beschreibung|Desc|Description)\s*[:=]\s*(.+)", raw, flags=re.IGNORECASE)
    if m:
        desc = m.group(1).strip()
    return {"url": url, "description": desc, "raw": raw.strip()}

def load_projects_file():
    if not PROJECTS_FILE.exists():
        return {}
    try:
        j = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
        if isinstance(j, dict):
            out = {}
            for k, v in j.items():
                if isinstance(v, dict):
                    out[k] = {"url": v.get("url", ""), "description": v.get("description", "")}
                else:
                    out[k] = {"url": "", "description": ""}
            return out
    except Exception:
        return {}
    return {}

def sse_event(event=None, data=None, comment=None):
    if comment is not None:
        return f": {comment}\n\n"
    msg = ""
    if event:
        msg += f"event: {event}\n"
    if data is not None:
        for line in str(data).splitlines():
            msg += f"data: {line}\n"
    msg += "\n"
    return msg

# --- Routes: UI ---
@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")

# --- Routes: Dateien ---
@app.get("/api/file")
def api_get_file():
    name = request.args.get("name", "update.json").strip()
    path = (BASE_DIR / name).resolve()
    if not str(path).startswith(str(BASE_DIR)):
        return fail("Pfad nicht erlaubt")
    if not path.exists():
        return fail("Datei nicht gefunden", 404)
    return ok({"name": name, "content": path.read_text(encoding="utf-8")})

@app.post("/api/file")
def api_save_file():
    payload = request.get_json(force=True, silent=True) or {}
    name = payload.get("name", "update.json")
    content = payload.get("content", "")
    path = (BASE_DIR / name).resolve()
    if not str(path).startswith(str(BASE_DIR)):
        return fail("Pfad nicht erlaubt")
    path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    return ok({"saved": name, "bytes": len(content)})

@app.get("/file/<path:fname>")
def serve_file(fname):
    fpath = (BASE_DIR / fname).resolve()
    if not str(fpath).startswith(str(BASE_DIR)):
        return fail("Pfad nicht erlaubt")
    if not fpath.exists():
        return fail("Datei nicht gefunden", 404)
    return send_from_directory(str(BASE_DIR), fname, as_attachment=False)

# --- Routes: Projekte (list, details, create, delete, patch) ---
@app.get("/api/projects")
def api_projects():
    file_projects = load_projects_file()
    names = list(file_projects.keys())
    if names:
        return ok({"projects": names, "source": "file"})
    if not SCRIPT.exists():
        return fail("Backend-Skript fehlt: gpt_spreadsheet_interface.py", 500)
    rc, out, _ = run_cli(["--list-projects"])
    if rc != 0:
        return fail("Auflistung fehlgeschlagen")
    return ok({"projects": parse_projects_list_from_cli_output(out), "source": "cli", "raw": out})

@app.get("/api/projects/<string:name>")
def api_project_details(name):
    file_projects = load_projects_file()
    if name in file_projects:
        d = file_projects[name]
        return ok({"name": name, "url": d.get("url", ""), "description": d.get("description", "")})
    rc, out, _ = run_cli(["--show", name])
    if rc != 0:
        return fail(f"Projekt '{name}' nicht gefunden oder Fehler beim Anzeigen")
    return ok(parse_show_details(out) | {"name": name})

@app.post("/api/projects")
def api_project_create():
    payload = request.get_json(force=True, silent=True) or {}
    name = payload.get("name", "").strip()
    url = payload.get("url", "").strip()
    desc = payload.get("desc", "").strip()
    if not name or not url or not desc:
        return fail("Erforderlich: name, url, desc")
    rc, out, _ = run_cli(["--new", name, "--url", url, "--desc", desc])
    if rc != 0:
        return fail("Projektanlage fehlgeschlagen")
    return ok({"created": name, "raw": out})

@app.delete("/api/projects/<string:name>")
def api_project_delete(name):
    rc, out, _ = run_cli(["--delete", name])
    if rc != 0:
        return fail("Löschen fehlgeschlagen")
    return ok({"deleted": name})

@app.patch("/api/projects/<string:name>")
def api_project_patch(name):
    payload = request.get_json(force=True, silent=True) or {}
    to = payload.get("to")
    url = payload.get("url")
    desc = payload.get("desc")
    last_out = None
    if to:
        rc, out, _ = run_cli(["--rename", name, "--to", to])
        if rc != 0:
            return fail("Umbenennen fehlgeschlagen")
        name = to
        last_out = out
    if url:
        rc, out, _ = run_cli(["--set-url", name, url])
        if rc != 0:
            return fail("URL-Update fehlgeschlagen")
        last_out = out
    if desc is not None:
        rc, out, _ = run_cli(["--set-desc", name, desc])
        if rc != 0:
            return fail("Beschreibung-Update fehlgeschlagen")
        last_out = out
    return ok({"name": name, "raw": last_out})

# --- Runs / SSE Konsole ---
_RUNS = {}  # run_id -> {"proc": Popen, "queue": Queue, "started": ts, "args": list}

# Nur Schreiboperationen serialisieren; reine Leseanfragen laufen ohne Lock parallel.
_WRITE_LOCK = threading.Lock()
_READ_ONLY_ACTIONS = frozenset({"get_tab", "get_headers", "find_rows", "find_first_empty", "list_tabs"})

def _payload_is_read_only(payload: dict) -> bool:
    actions = payload.get("actions") or []
    return bool(actions) and all(a.get("type") in _READ_ONLY_ACTIONS for a in actions)

@app.post("/api/exec")
def api_exec():
    """Synchroner Sheets-Aufruf für Agenten. Payload = update.json-Inhalt, Antwort = status.json."""
    payload = request.get_json(force=True, silent=True) or {}

    def _exec():
        if _IN_PROCESS:
            try:
                status = _run_payload(payload)
                return ok({"returncode": 0, "status": status, "cli_output": ""})
            except Exception as exc:
                return fail(f"Engine-Fehler: {exc}", 500)
        # Legacy-Fallback via subprocess
        project = payload.get("project")
        ensure_files_exist()
        UPDATE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        args = ["--project", project] if project else []
        rc, out, _ = run_cli(args)
        status = {}
        if STATUS_FILE.exists():
            try:
                status = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return ok({"returncode": rc, "status": status, "cli_output": out})

    if _payload_is_read_only(payload):
        return _exec()
    with _WRITE_LOCK:
        return _exec()

@app.post("/api/run")
def api_run():
    payload = request.get_json(force=True, silent=True) or {}
    project = payload.get("project") or None
    args = ["--debug"]
    if project:
        args = ["--project", project, "--debug"]
    if not SCRIPT.exists():
        return fail("Backend-Skript fehlt: gpt_spreadsheet_interface.py", 500)
    proc, q = run_cli(args, stream=True)
    run_id = str(uuid.uuid4())
    _RUNS[run_id] = {"proc": proc, "queue": q, "started": time.time(), "args": args}
    return ok({"run_id": run_id, "args": args, "pid": proc.pid})

@app.get("/api/stream/<string:run_id>")
def api_stream(run_id):
    info = _RUNS.get(run_id)
    if not info:
        return fail("Unbekannte run_id", 404)

    def gen():
        # Erste Bytes früh senden (SSE-Handshake + sichtbarer Start)
        yield sse_event(comment="connected")
        yield sse_event(event="line", data="SSE verbunden")
        yield sse_event(event="start", data=json.dumps({"args": info["args"], "pid": info["proc"].pid}))
        q = info["queue"]
        last_ping = time.time()
        while True:
            try:
                line = q.get(timeout=1.0)
            except queue.Empty:
                # Heartbeat, um Verbindungen offen zu halten
                yield sse_event(comment="ping")
                last_ping = time.time()
                continue
            if line is None:
                rc = info["proc"].wait()
                yield sse_event(event="end", data=json.dumps({"returncode": rc, "ts": time.time()}))
                break
            yield sse_event(event="line", data=line)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(gen(), mimetype="text/event-stream", headers=headers)

@app.get("/api/status-ready")
def api_status_ready():
    exists = STATUS_FILE.exists() and STATUS_FILE.stat().st_size > 0
    ts = STATUS_FILE.stat().st_mtime if exists else None
    return ok({"exists": exists, "modified": ts, "path": f"/file/{STATUS_FILE.name}" if exists else None})

# --- Health ---
@app.get("/healthz")
def health():
    return ok({"cwd": str(BASE_DIR), "script_exists": SCRIPT.exists()})

# --- Start ---
def main():
    ensure_files_exist()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True, use_reloader=False)

# --- UI ---
INDEX_HTML = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>owAPI – Web GUI</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0b0f14;--panel:#0f1520;--accent:#4da3ff;--muted:#8aa0b3;--text:#e6eef7;--ok:#3ecf8e;--err:#ff6b6b}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif;background:var(--bg);color:var(--text)}
header{padding:16px 20px;border-bottom:1px solid #1b2230;display:flex;align-items:center;gap:16px}
h1{font-size:18px;margin:0}
small.badge{background:#162235;color:var(--muted);padding:4px 8px;border-radius:999px}
main{display:grid;grid-template-columns:340px 1fr;gap:16px;padding:16px}
.panel{background:var(--panel);border:1px solid #1b2230;border-radius:14px;overflow:hidden}
.panel h2{font-size:14px;margin:0;padding:10px 12px;border-bottom:1px solid #1b2230;color:var(--muted)}
.panel .body{padding:12px}
label{display:block;font-size:12px;color:var(--muted);margin:8px 0 4px}
input[type=text], textarea{width:100%;background:#0b1220;border:1px solid #1b2230;color:var(--text);border-radius:10px;padding:10px;font-family:ui-monospace,Consolas,Menlo,monospace}
textarea#editor{height:320px;white-space:pre}
button{background:var(--accent);color:#031222;border:none;border-radius:10px;padding:10px 12px;font-weight:600;cursor:pointer}
button.flat{background:#172237;color:var(--text);border:1px solid #1b2230}
button:disabled{opacity:.5;cursor:not-allowed}
.row{display:flex;gap:8px;align-items:center}
.list{display:flex;flex-direction:column;gap:6px;max-height:240px;overflow:auto}
.item{padding:8px;border:1px solid #1b2230;border-radius:10px;display:flex;justify-content:space-between;gap:8px;align-items:center}
.item.sel{border-color:var(--accent);box-shadow:0 0 0 2px rgba(77,163,255,.2) inset}
small{color:var(--muted)}
pre.console{background:#000;border-top:1px solid #1b2230;margin:0;padding:12px;max-height:420px;overflow:auto;font-size:12px;line-height:1.35}
a.link{color:var(--accent);text-decoration:none}
.sep{height:8px}
.badge-ok{color:var(--ok);font-weight:600}
.badge-err{color:var(--err);font-weight:600}
footer{padding:10px 16px;color:var(--muted);border-top:1px solid #1b2230}
code.k{background:#0b1220;border:1px solid #1b2230;border-radius:6px;padding:2px 6px}
h3.sub{font-size:13px;margin:0 0 6px 0;color:var(--muted)}
.col2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
</style>
</head>
<body>
<header>
  <h1>owAPI – Web GUI</h1>
  <small class="badge">Port 8123</small>
</header>

<main>
  <section class="panel">
    <h2>Projekte</h2>
    <div class="body">
      <div class="row">
        <button class="flat" id="btn-refresh">Aktualisieren</button>
        <button class="flat" id="btn-show">Details</button>
      </div>
      <div class="sep"></div>
      <div class="list" id="project-list"></div>
      <div class="sep"></div>
      <div id="project-details">
        <label>Ausgewähltes Projekt</label>
        <div><strong id="sel-name">—</strong></div>
        <div><small>URL: <a id="sel-url" class="link" href="#" target="_blank">—</a></small></div>
        <div><small>Beschreibung: <span id="sel-desc">—</span></small></div>
      </div>
      <div class="sep"></div>
      <h3 class="sub">Neues Projekt anlegen</h3>
      <label>Name</label><input id="new-name" type="text" placeholder="Projektname">
      <label>URL</label><input id="new-url" type="text" placeholder="https://…">
      <label>Beschreibung</label><input id="new-desc" type="text" placeholder="Kurzbeschreibung">
      <div class="sep"></div>
      <button id="btn-create">Anlegen</button>
      <div class="sep"></div>
      <h3 class="sub">Projekt ändern</h3>
      <div class="col2">
        <div>
          <label>Umbenennen zu</label>
          <input id="rename-to" type="text" placeholder="Neuer Name">
        </div>
        <div style="align-self:end">
          <button class="flat" id="btn-rename">Umbenennen</button>
        </div>
      </div>
      <label>Neue URL</label><input id="edit-url" type="text" placeholder="https://…">
      <label>Neue Beschreibung</label><input id="edit-desc" type="text" placeholder="Beschreibung">
      <div class="row">
        <button class="flat" id="btn-update">URL/Beschreibung speichern</button>
        <button class="flat" id="btn-delete" style="margin-left:auto">Löschen</button>
      </div>
      <div class="sep"></div>
      <small id="proj-msg"></small>
    </div>
  </section>

  <section class="panel" style="grid-column: span 1">
    <h2>update.json bearbeiten &amp; ausführen</h2>
    <div class="body">
      <div class="row">
        <button class="flat" id="btn-load-update">update.json laden</button>
        <button id="btn-save-update">Speichern</button>
        <button class="flat" id="btn-clear" style="margin-left:auto">Konsole leeren</button>
        <label style="margin-left:8px;display:flex;align-items:center;gap:6px">
          <input id="chk-use-project" type="checkbox"> <span>Ausgewähltes Projekt verwenden</span>
        </label>
      </div>
      <div class="sep"></div>
      <textarea id="editor" spellcheck="false" placeholder="{\n  // Inhalt von update.json\n}"></textarea>
      <div class="sep"></div>
      <div class="row">
        <button id="btn-run">▶ Ausführen ( --debug )</button>
        <a id="status-link" class="link" href="#" target="_blank" style="margin-left:12px">status.json öffnen</a>
        <small id="status-badge">status.json: unbekannt</small>
      </div>
    </div>
    <pre class="console" id="console"></pre>
  </section>
</main>

<footer>
  <small>Steuert <code class="k">gpt_spreadsheet_interface.py</code> im gleichen Arbeitsverzeichnis. Ausgabe erscheint live in der Konsole.</small>
</footer>

<script>
const el = (id)=>document.getElementById(id);
const list = el('project-list');
let selected = null;

async function api(url, opts){
  const r = await fetch(url, opts);
  const j = await r.json();
  if(!j.ok) throw new Error(j.error || 'API-Fehler');
  return j.data ?? j;
}

function renderProjects(items){
  list.innerHTML = '';
  items.forEach(name=>{
    const d = document.createElement('div');
    d.className = 'item';
    d.innerHTML = `<div>${name}</div><div class="row"><button class="flat btn-pick">Wählen</button><button class="flat btn-del">✕</button></div>`;
    d.querySelector('.btn-pick').onclick = ()=>{
      [...list.children].forEach(c=>c.classList.remove('sel'));
      d.classList.add('sel');
      selected = name;
      el('sel-name').textContent = name;
      el('sel-url').textContent = '—'; el('sel-url').href = '#';
      el('sel-desc').textContent = '—';
      el('edit-url').value = ''; el('edit-desc').value = ''; el('rename-to').value='';
    };
    d.querySelector('.btn-del').onclick = async ()=>{
      if(!confirm(`Projekt "${name}" löschen?`)) return;
      try{
        await api('/api/projects/'+encodeURIComponent(name), {method:'DELETE'});
        if(selected===name){ selected=null; el('sel-name').textContent='—'; el('sel-url').textContent='—'; el('sel-url').href='#'; el('sel-desc').textContent='—'; }
        await refreshProjects();
        el('proj-msg').textContent='Projekt gelöscht';
      }catch(e){ el('proj-msg').textContent='Fehler: '+e.message; }
    };
    list.appendChild(d);
  });
}

async function refreshProjects(){
  try{
    const res = await api('/api/projects');
    renderProjects(res.projects||[]);
    el('proj-msg').textContent = '';
  }catch(e){
    el('proj-msg').textContent = 'Fehler: ' + e.message;
  }
}

async function showDetails(){
  if(!selected){ el('proj-msg').textContent='Kein Projekt gewählt'; return; }
  try{
    const d = await api('/api/projects/'+encodeURIComponent(selected));
    el('sel-name').textContent = d.name || selected;
    el('sel-url').textContent = d.url || '—';
    el('sel-url').href = d.url || '#';
    el('sel-desc').textContent = d.description || '—';
    el('proj-msg').textContent = '';
  }catch(e){
    el('proj-msg').textContent = 'Details-Fehler: ' + e.message;
  }
}

async function loadUpdate(){
  try{
    const d = await api('/api/file?name=update.json');
    el('editor').value = d.content || '';
  }catch(e){
    alert('Konnte update.json nicht laden: '+e.message);
  }
}

async function saveUpdate(){
  try{
    const body = {name:'update.json', content: el('editor').value};
    await api('/api/file', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  }catch(e){
    alert('Speichern fehlgeschlagen: '+e.message);
  }
}

function appendConsole(txt){
  const c = el('console');
  c.textContent += (txt.endsWith('\n')?txt:txt+'\n');
  c.scrollTop = c.scrollHeight;
}

function clearConsole(){
  el('console').textContent = '';
}

async function runScript(){
  appendConsole('--- Starte: gpt_spreadsheet_interface.py --debug ---');
  await saveUpdate();

  const useProject = el('chk-use-project').checked;
  const payload = useProject && selected ? {project: selected} : {};
  let run_id;
  try{
    const r = await api('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    run_id = r.run_id;
    appendConsole('PID: '+r.pid+'  Args: '+(r.args||[]).join(' '));
  }catch(e){
    appendConsole('[FEHLER] Start fehlgeschlagen: '+e.message);
    return;
  }

  const es = new EventSource('/api/stream/'+run_id);
  // Fallback auf "message"
  es.onmessage = (ev)=>{ if(ev.data) appendConsole(ev.data); };
  es.addEventListener('start', ev=>{
    try{ const j = JSON.parse(ev.data); appendConsole('Args: '+(j.args||[]).join(' ')+'  PID:'+j.pid); }catch(_){}
  });
  es.addEventListener('line', ev=>{ appendConsole(ev.data); });
  es.addEventListener('end', async ev=>{
    es.close();
    try{ const j = JSON.parse(ev.data); appendConsole('--- Ende (rc='+j.returncode+') ---'); }catch(_){ appendConsole('--- Ende ---'); }
    await refreshStatus();
  });
  es.onerror = ()=>{/* stillhalten */};
}

async function refreshStatus(){
  try{
    const d = await api('/api/status-ready');
    if(d.exists){
      el('status-link').href = d.path;
      el('status-badge').textContent = 'status.json: bereit ('+ new Date(d.modified*1000).toLocaleString()+')';
      el('status-badge').className='badge-ok';
    }else{
      el('status-badge').textContent = 'status.json: nicht vorhanden';
      el('status-badge').className='badge-err';
    }
  }catch(e){
    el('status-badge').textContent = 'status.json: Fehler';
    el('status-badge').className='badge-err';
  }
}

async function createProject(){
  const name = el('new-name').value.trim();
  const url  = el('new-url').value.trim();
  const desc = el('new-desc').value.trim();
  if(!name||!url||!desc){ el('proj-msg').textContent='Bitte Name/URL/Beschreibung ausfüllen'; return; }
  try{
    await api('/api/projects', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, url, desc})});
    el('proj-msg').textContent='Projekt angelegt';
    el('new-name').value=''; el('new-url').value=''; el('new-desc').value='';
    await refreshProjects();
  }catch(e){
    el('proj-msg').textContent='Fehler: '+e.message;
  }
}

async function patchProject(body){
  if(!selected){ el('proj-msg').textContent='Kein Projekt gewählt'; return; }
  try{
    await api('/api/projects/'+encodeURIComponent(selected), {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    if(body.to){ selected = body.to; }
    el('proj-msg').textContent='Gespeichert';
    await refreshProjects();
    await showDetails();
  }catch(e){
    el('proj-msg').textContent='Fehler: '+e.message;
  }
}

document.addEventListener('DOMContentLoaded', async ()=>{
  el('btn-refresh').onclick = refreshProjects;
  el('btn-show').onclick = showDetails;
  el('btn-load-update').onclick = loadUpdate;
  el('btn-save-update').onclick = saveUpdate;
  el('btn-run').onclick = runScript;
  el('btn-create').onclick = createProject;
  el('btn-clear').onclick = clearConsole;
  el('btn-rename').onclick = ()=>patchProject({to: el('rename-to').value.trim()});
  el('btn-update').onclick = ()=>patchProject({url: el('edit-url').value.trim() || undefined, desc: el('edit-desc').value});

  await refreshProjects();
  await loadUpdate();
  await refreshStatus();
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
