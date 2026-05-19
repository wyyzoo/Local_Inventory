#!/usr/bin/env python3
"""
Inventory Portal — Canada Local Warehouse
Data source: local SQLite database (inventory.db) only.
Google Sheet is the manual archive — this portal does not read or write it.
"""

import os, re, threading, webbrowser, logging, sqlite3, base64
from datetime import date
from functools import wraps
import warnings; warnings.filterwarnings('ignore')

try:
    import flask
except ImportError:
    import subprocess, sys
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'flask', '--quiet'], check=True)
    import flask

PORT     = 5050
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE  = os.path.join(BASE_DIR, 'inventory.db')

# ── Helpers ───────────────────────────────────────────────────────────────────
def _to_float(v):
    try: return float(v)
    except: return None

def _build_remark(customer, order_num, code, notes):
    parts = []
    if customer or order_num:
        base = 'Return from'
        if customer:  base += f' {customer}'
        if order_num: base += f' ({order_num})'
        parts.append(base)
    if code:  parts.append(code.upper().strip())
    if notes: parts.append(notes.strip())
    return ', '.join(parts) if parts else 'Check-in'

# ── Database ───────────────────────────────────────────────────────────────────
def init_db():
    """Create tables if missing; add booking columns if upgrading an older DB."""
    with sqlite3.connect(DB_FILE) as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS inventory (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                sheet_row INTEGER,
                date      TEXT,
                model     TEXT,
                version   TEXT,
                category  TEXT,
                rating    REAL,
                code      TEXT,
                customer  TEXT,
                ord       TEXT,
                remark    TEXT
            );
            CREATE TABLE IF NOT EXISTS history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                date      TEXT,
                model     TEXT,
                version   TEXT,
                category  TEXT,
                rating    REAL,
                code      TEXT,
                customer  TEXT,
                ord       TEXT,
                direction TEXT,
                reason    TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        # Booking columns — safe migration (no-op if already present)
        for col, defn in [
            ('status',          "TEXT NOT NULL DEFAULT 'available'"),
            ('booked_customer', "TEXT NOT NULL DEFAULT ''"),
            ('booked_order',    "TEXT NOT NULL DEFAULT ''"),
            ('booked_purpose',  "TEXT NOT NULL DEFAULT ''"),
            ('booked_date',     "TEXT NOT NULL DEFAULT ''"),
            ('packaging',       "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                con.execute(f'ALTER TABLE inventory ADD COLUMN {col} {defn}')
            except sqlite3.OperationalError:
                pass  # column already exists

def db_read_inventory():
    with sqlite3.connect(DB_FILE) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            'SELECT id AS row, date, model, version, category, rating, '
            'code, customer, ord AS "order", remark, '
            'status, booked_customer, booked_order, booked_purpose, booked_date, packaging '
            'FROM inventory'
        ).fetchall()
        return [dict(r) for r in rows]

def db_read_history():
    with sqlite3.connect(DB_FILE) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            'SELECT date, model, version, category, rating, code, customer, '
            'ord AS "order", direction, reason FROM history ORDER BY date DESC, id DESC'
        ).fetchall()
        return [dict(r) for r in rows]

# ── Operations ────────────────────────────────────────────────────────────────
def process_bookout(item_ids, customer, order, purpose):
    today = date.today().strftime('%Y-%m-%d')
    ph = ','.join('?' * len(item_ids))
    with sqlite3.connect(DB_FILE) as con:
        con.execute(
            f"UPDATE inventory SET status='booked', booked_customer=?, booked_order=?, "
            f"booked_purpose=?, booked_date=? WHERE id IN ({ph})",
            [customer, order, purpose, today] + list(item_ids)
        )

def process_putback(item_ids):
    ph = ','.join('?' * len(item_ids))
    with sqlite3.connect(DB_FILE) as con:
        con.execute(
            f"UPDATE inventory SET status='available', booked_customer='', "
            f"booked_order='', booked_purpose='', booked_date='' WHERE id IN ({ph})",
            list(item_ids)
        )

def process_complete(item_ids):
    today = date.today().strftime('%Y-%m-%d')
    ph    = ','.join('?' * len(item_ids))
    with sqlite3.connect(DB_FILE) as con:
        con.row_factory = sqlite3.Row
        items = con.execute(
            f'SELECT * FROM inventory WHERE id IN ({ph})', list(item_ids)
        ).fetchall()
        con.execute(f'DELETE FROM inventory WHERE id IN ({ph})', list(item_ids))
        for item in items:
            con.execute(
                'INSERT INTO history '
                '(date,model,version,category,rating,code,customer,ord,direction,reason) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                [today, item['model'], item['version'], item['category'], item['rating'],
                 item['code'], item['booked_customer'] or '', item['booked_order'] or '',
                 'out', item['booked_purpose'] or 'Checkout']
            )

def process_update(item_id, model, version, category, rating, customer, order_num, code, remark, date_val, packaging):
    rating_float = _to_float(str(rating).strip() if rating else '')
    code_clean   = code.upper().strip() if code else ''
    with sqlite3.connect(DB_FILE) as con:
        con.execute(
            'UPDATE inventory SET model=?, version=?, category=?, rating=?, '
            'code=?, customer=?, ord=?, remark=?, date=?, packaging=? WHERE id=?',
            [model.strip(), version.strip(), category.strip(), rating_float,
             code_clean, customer.strip(), order_num.strip(), remark.strip(),
             date_val.strip() if date_val else '', packaging.strip(), item_id]
        )

def process_checkin(model, version, category, rating, customer, order_num, code, notes, packaging):
    today        = date.today().strftime('%Y-%m-%d')
    remark       = _build_remark(customer, order_num, code, notes)
    rating_float = _to_float(str(rating).strip() if rating else '')
    code_clean   = code.upper().strip() if code else ''
    with sqlite3.connect(DB_FILE) as con:
        con.execute(
            'INSERT INTO inventory '
            '(date,model,version,category,rating,code,customer,ord,remark,packaging) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            [today, model.strip(), version.strip(), category.strip(),
             rating_float, code_clean, customer.strip(), order_num.strip(), remark,
             packaging.strip()]
        )
        reason = 'Return' if customer.strip() else 'Check-in'
        con.execute(
            'INSERT INTO history '
            '(date,model,version,category,rating,code,customer,ord,direction,reason) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            [today, model.strip(), version.strip(), category.strip(),
             rating_float, code_clean, customer.strip(), order_num.strip(), 'in', reason]
        )

# ── Flask ─────────────────────────────────────────────────────────────────────
app = flask.Flask(__name__)
app.secret_key = 'inv-portal-sk-2026'
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# ── Session Auth ──────────────────────────────────────────────────────────────
# role: 'admin' = full access, 'readonly' = view only
USERS = {
    'pi':    ('inventory2026', 'readonly'),
    'tzhao': ('P@55w0rd',      'admin'),
}

LOGIN_PAGE = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Inventory Portal — Login</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f0f4ff;display:flex;align-items:center;justify-content:center;
       min-height:100vh}
  .card{background:#fff;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.08);
        padding:40px 36px;width:340px;max-width:92vw}
  .logo{display:flex;align-items:center;gap:10px;margin-bottom:28px}
  .logo-icon{font-size:28px}
  .logo-text{font-size:17px;font-weight:700;color:#1e293b;line-height:1.2}
  .logo-sub{font-size:12px;color:#64748b;font-weight:400}
  label{display:block;font-size:12px;font-weight:600;color:#475569;margin-bottom:5px}
  input{width:100%;border:1.5px solid #e2e8f0;border-radius:7px;padding:9px 12px;
        font-size:14px;outline:none;transition:border .15s}
  input:focus{border-color:#0d9488}
  .field{margin-bottom:16px}
  .err{color:#dc2626;font-size:12px;margin-bottom:14px;display:none}
  .err.show{display:block}
  button{width:100%;background:#0d9488;color:#fff;border:none;border-radius:7px;
         padding:10px;font-size:14px;font-weight:600;cursor:pointer;margin-top:4px}
  button:hover{background:#0f766e}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-icon">📦</div>
    <div class="logo-text">Local Inventory<div class="logo-sub">Canada Warehouse</div></div>
  </div>
  <form method="POST" action="/login">
    <div class="field">
      <label>Username</label>
      <input name="username" type="text" autocomplete="username" autofocus>
    </div>
    <div class="field">
      <label>Password</label>
      <input name="password" type="password" autocomplete="current-password">
    </div>
    <div class="err {err_class}">{err_msg}</div>
    <button type="submit">Sign In</button>
  </form>
</div>
</body>
</html>"""

@app.before_request
def check_auth():
    if flask.request.path in ('/login', '/logout'):
        return
    user = flask.session.get('user')
    if user and user in USERS:
        flask.g.user = user
        flask.g.role = USERS[user][1]
        return
    return flask.redirect('/login')

@app.route('/login', methods=['GET', 'POST'])
def login():
    err_class, err_msg = '', ''
    if flask.request.method == 'POST':
        u = flask.request.form.get('username', '').strip()
        p = flask.request.form.get('password', '')
        if u in USERS and USERS[u][0] == p:
            flask.session['user'] = u
            return flask.redirect('/')
        err_class, err_msg = 'show', 'Invalid username or password.'
    return LOGIN_PAGE.replace('{err_class}', err_class).replace('{err_msg}', err_msg)

@app.route('/logout')
def logout():
    flask.session.clear()
    return flask.redirect('/login')

@app.route('/api/whoami')
def api_whoami():
    return flask.jsonify({'user': flask.g.user, 'role': flask.g.role})

def _admin_only():
    if getattr(flask.g, 'role', None) != 'admin':
        return flask.jsonify({'ok': False, 'error': 'Read-only access.'}), 403
    return None

@app.route('/api/inventory')
def api_inventory():
    try:
        return flask.jsonify({'ok': True, 'items': db_read_inventory()})
    except Exception as e:
        return flask.jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/history')
def api_history():
    try:
        return flask.jsonify({'ok': True, 'items': db_read_history()})
    except Exception as e:
        return flask.jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/bookout', methods=['POST'])
def api_bookout():
    err = _admin_only()
    if err: return err
    body    = flask.request.get_json()
    ids     = body.get('rows', [])
    purpose = body.get('purpose', '').strip()
    if not ids:
        return flask.jsonify({'ok': False, 'error': 'No cameras selected.'}), 400
    if not purpose:
        return flask.jsonify({'ok': False, 'error': 'Purpose is required.'}), 400
    try:
        process_bookout(ids, body.get('customer','').strip(),
                        body.get('order','').strip(), purpose)
        return flask.jsonify({'ok': True, 'count': len(ids)})
    except Exception as e:
        return flask.jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/putback', methods=['POST'])
def api_putback():
    err = _admin_only()
    if err: return err
    ids = flask.request.get_json().get('rows', [])
    if not ids:
        return flask.jsonify({'ok': False, 'error': 'No cameras selected.'}), 400
    try:
        process_putback(ids)
        return flask.jsonify({'ok': True, 'count': len(ids)})
    except Exception as e:
        return flask.jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/complete', methods=['POST'])
def api_complete():
    err = _admin_only()
    if err: return err
    ids = flask.request.get_json().get('rows', [])
    if not ids:
        return flask.jsonify({'ok': False, 'error': 'No cameras selected.'}), 400
    try:
        process_complete(ids)
        return flask.jsonify({'ok': True, 'count': len(ids)})
    except Exception as e:
        return flask.jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/update', methods=['POST'])
def api_update():
    err = _admin_only()
    if err: return err
    body = flask.request.get_json()
    item_id  = body.get('id')
    model    = body.get('model', '').strip()
    category = body.get('category', '').strip()
    if not item_id:
        return flask.jsonify({'ok': False, 'error': 'Missing item id.'}), 400
    if not model or not category:
        return flask.jsonify({'ok': False, 'error': 'Model and Category are required.'}), 400
    try:
        process_update(item_id, model,
                       body.get('version', '').strip(),
                       category,
                       body.get('rating', ''),
                       body.get('customer', '').strip(),
                       body.get('order', '').strip(),
                       body.get('code', '').strip(),
                       body.get('remark', '').strip(),
                       body.get('date', '').strip(),
                       body.get('packaging', '').strip())
        return flask.jsonify({'ok': True})
    except Exception as e:
        return flask.jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/checkin', methods=['POST'])
def api_checkin():
    err = _admin_only()
    if err: return err
    body     = flask.request.get_json()
    model    = body.get('model', '').strip()
    category = body.get('category', '').strip()
    if not model or not category:
        return flask.jsonify({'ok': False, 'error': 'Model and Category are required.'}), 400
    try:
        process_checkin(model,
                        body.get('version', '').strip(),
                        category,
                        body.get('rating', ''),
                        body.get('customer', '').strip(),
                        body.get('order', '').strip(),
                        body.get('code', '').strip(),
                        body.get('notes', '').strip(),
                        body.get('packaging', '').strip())
        return flask.jsonify({'ok': True})
    except Exception as e:
        return flask.jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/')
def index():
    return flask.Response(HTML, content_type='text/html; charset=utf-8')

# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Local Inventory — Canada</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --navy:#1F3864;--teal:#2F5496;--teal2:#4472C4;
  --border:#dde1e7;--bg:#f4f6fb;--card:#fff;
  --text:#1a1d23;--muted:#6b7280;--sel:#2F5496;
  --ln-bg:#dbeafe;--ln-txt:#1e40af;
  --nw-bg:#dcfce7;--nw-txt:#166534;
  --rp-bg:#ffedd5;--rp-txt:#9a3412;
  --bk-bg:#fef3c7;--bk-txt:#92400e;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;
  font-size:13px;color:var(--text)
}
body{background:var(--bg);height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* Header */
.hdr{background:linear-gradient(120deg,#1a305a 0%,#2f5496 100%);padding:0 20px;height:54px;
     display:flex;align-items:center;justify-content:space-between;flex-shrink:0;user-select:none}
.hdr-left{display:flex;align-items:center;gap:14px}
.hdr-title{color:rgba(255,255,255,.75);font-size:11.5px;font-weight:400;display:flex;align-items:center;gap:8px}
.nav-tabs{display:flex;gap:2px}
.nav-tab{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.15);border-radius:6px;
         padding:5px 16px;font-size:12.5px;font-weight:500;color:rgba(255,255,255,.75);
         cursor:pointer;transition:all .15s}
.nav-tab:hover{background:rgba(255,255,255,.18);color:#fff}
.nav-tab.active{background:rgba(255,255,255,.25);color:#fff;border-color:rgba(255,255,255,.35)}
.hdr-stats{display:flex;gap:8px}
.stat{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.18);border-radius:20px;
      padding:4px 13px;display:flex;align-items:center;gap:7px;font-size:12px;color:rgba(255,255,255,.85)}
.stat-n{font-weight:700;font-size:15px;color:#fff}
.stat-dot{width:7px;height:7px;border-radius:50%}
.stat.ln .stat-dot{background:#93c5fd}
.stat.rp .stat-dot{background:#fdba74}
.stat.nw .stat-dot{background:#86efac}
.stat.clickable{cursor:pointer;transition:background .15s,box-shadow .15s}
.stat.clickable:hover{background:rgba(255,255,255,.22)}
.stat.clickable.active{background:rgba(255,255,255,.32);box-shadow:0 0 0 2px rgba(255,255,255,.6)}
.stat.bk .stat-dot{background:#fcd34d}

/* Toolbar */
.toolbar{background:var(--card);border-bottom:1px solid var(--border);padding:9px 20px;
         display:flex;align-items:center;gap:8px;flex-shrink:0}
.search{position:relative;flex:1;max-width:240px;min-width:130px}
.search input{width:100%;padding:6px 10px 6px 30px;border:1px solid var(--border);border-radius:6px;
              font-size:12.5px;outline:none;background:var(--bg);transition:border .15s,box-shadow .15s}
.search input:focus{border-color:var(--teal2);box-shadow:0 0 0 3px rgba(68,114,196,.15)}
.search svg{position:absolute;left:8px;top:50%;transform:translateY(-50%);color:var(--muted);pointer-events:none}
.filters{display:flex;gap:3px}
.fb{border:1px solid var(--border);border-radius:5px;padding:4px 10px;font-size:11.5px;font-weight:500;
    cursor:pointer;background:var(--card);color:var(--muted);transition:all .14s;white-space:nowrap}
.fb:hover{border-color:var(--teal2);color:var(--teal)}
.fb.on{background:var(--teal);color:#fff;border-color:var(--teal)}
.fb.in.on{background:#166534;border-color:#166634}
.fb.out.on{background:#c2410c;border-color:#c2410c}
.fb.ln.on{background:#1d4ed8;border-color:#1d4ed8}
.fb.rp.on{background:#c2410c;border-color:#c2410c}
.fb.nw.on{background:#15803d;border-color:#15803d}
.fb.bk.on{background:#b45309;border-color:#b45309}
.fb-cnt{font-weight:700;margin-left:4px;opacity:.85}
.sp{flex:1}
.tb{border:1px solid var(--border);border-radius:5px;padding:4px 10px;font-size:11.5px;
    cursor:pointer;background:var(--card);color:var(--muted);transition:all .14s;white-space:nowrap}
.tb:hover{background:#f0f4ff;color:var(--teal)}
.btn-ci{background:#166534;color:#fff;border:none;border-radius:6px;padding:5px 14px;
        font-size:12px;font-weight:600;cursor:pointer;display:flex;align-items:center;gap:5px;
        transition:background .15s;white-space:nowrap}
.btn-ci:hover{background:#14532d}
.btn-refresh{background:none;border:1px solid var(--border);border-radius:6px;padding:5px 12px;
             font-size:12px;cursor:pointer;color:var(--muted);display:flex;align-items:center;gap:5px;
             transition:all .14s}
.btn-refresh:hover{background:#f0f4ff;color:var(--teal);border-color:var(--teal2)}
.meta-txt{font-size:11.5px;color:var(--muted);white-space:nowrap}

/* Table */
.tbl-wrap{flex:1;overflow:auto;background:var(--card)}
table{width:100%;border-collapse:collapse;font-size:12.5px}
thead th{position:sticky;top:0;z-index:5;background:#f1f3f8;border-bottom:2px solid var(--border);
         padding:7px 10px;font-weight:600;color:var(--muted);text-align:left;
         cursor:pointer;user-select:none;white-space:nowrap}
thead th.ns{cursor:default}
thead th:hover:not(.ns){color:var(--teal)}
th.sa::after{content:' ↑';color:var(--teal2)}
th.sd::after{content:' ↓';color:var(--teal2)}
tbody tr{border-bottom:1px solid #f0f1f4;transition:background .1s}
tbody tr.clickable{cursor:pointer}
tbody tr.clickable:hover:not(.sel){background:#f5f7ff}
tbody tr.sel{background:var(--sel);color:#fff}
tbody tr.row-booked{background:#fffbeb}
tbody tr.row-booked:hover:not(.sel){background:#fef3c7}
tbody tr.row-in{background:#f0fdf4}
tbody tr.row-in:hover{background:#dcfce7}
tbody tr.row-out{background:#fff7ed}
tbody tr.row-out:hover{background:#ffedd5}
td{padding:7px 10px;vertical-align:middle}
input[type=checkbox]{width:14px;height:14px;cursor:pointer;accent-color:var(--teal)}
tr.sel input[type=checkbox]{accent-color:#fff}
.badge{display:inline-block;border-radius:10px;padding:2px 9px;font-size:11px;font-weight:600;white-space:nowrap}
.badge.ln{background:var(--ln-bg);color:var(--ln-txt)}
.badge.nw{background:var(--nw-bg);color:var(--nw-txt)}
.badge.rp{background:var(--rp-bg);color:var(--rp-txt)}
.badge.bk{background:var(--bk-bg);color:var(--bk-txt)}
.badge.in-b{background:#dcfce7;color:#166534}
.badge.out-b{background:#ffedd5;color:#9a3412}
.badge.ret-b{background:#dbeafe;color:#1e40af}
.badge.rep-b{background:#ffedd5;color:#9a3412}
.badge.sal-b{background:#f3e8ff;color:#6b21a8}
.badge.oth-b{background:#f1f5f9;color:#475569}
tr.sel .badge{background:rgba(255,255,255,.22);color:#fff}
.mono{font-family:'SF Mono','Fira Code',monospace;font-size:11.5px;letter-spacing:.5px;font-weight:600}
.rtg{text-align:center;font-weight:600}
.empty{text-align:center;padding:60px;color:var(--muted);font-size:14px}
.booked-to{font-size:11px;color:#92400e;margin-top:2px}

/* Action bar */
.ckbar{background:var(--card);border-top:2px solid var(--border);padding:10px 20px;
       display:flex;align-items:center;gap:12px;flex-shrink:0;min-height:56px}
.sel-lbl{font-weight:600;font-size:13px;min-width:100px;color:var(--navy);white-space:nowrap}
.sel-lbl b{font-size:17px;color:var(--teal2)}
.vdiv{width:1px;height:28px;background:var(--border);flex-shrink:0}
.bar-state{display:flex;align-items:center;gap:10px}
.sp2{flex:1}
.bar-hint{font-size:12px;color:var(--muted)}
.bar-warn{font-size:12px;color:#c2410c;font-weight:500}

/* Buttons */
.btn-bookout{background:#b45309;color:#fff;border:none;border-radius:6px;padding:7px 18px;
             font-size:13px;font-weight:600;cursor:pointer;display:flex;align-items:center;gap:6px;
             transition:background .15s}
.btn-bookout:hover:not(:disabled){background:#92400e}
.btn-bookout:disabled{opacity:.4;cursor:not-allowed}
.btn-putback{background:none;border:1.5px solid var(--border);border-radius:6px;padding:7px 16px;
             font-size:13px;font-weight:500;cursor:pointer;color:var(--text);
             display:flex;align-items:center;gap:6px;transition:all .15s}
.btn-putback:hover:not(:disabled){border-color:var(--teal2);background:#f0f4ff;color:var(--teal)}
.btn-putback:disabled{opacity:.4;cursor:not-allowed}
.btn-complete{background:#166534;color:#fff;border:none;border-radius:6px;padding:7px 18px;
              font-size:13px;font-weight:600;cursor:pointer;display:flex;align-items:center;gap:6px;
              transition:background .15s}
.btn-complete:hover:not(:disabled){background:#14532d}
.btn-complete:disabled{opacity:.4;cursor:not-allowed}
.btn-edit{background:none;border:1.5px solid var(--border);border-radius:6px;padding:7px 16px;
          font-size:13px;font-weight:500;cursor:pointer;color:var(--text);
          display:flex;align-items:center;gap:6px;transition:all .15s}
.btn-edit:hover{border-color:var(--teal2);background:#f0f4ff;color:var(--teal)}

/* Stats bar (history) */
.statsbar{background:var(--card);border-top:2px solid var(--border);padding:9px 20px;
          display:flex;align-items:center;gap:16px;flex-shrink:0}
.sbar-item{display:flex;align-items:center;gap:7px;font-size:12.5px}
.sbar-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.sbar-dot.in{background:#166534}
.sbar-dot.out{background:#c2410c}
.sbar-dot.net{background:var(--teal)}
.sbar-val{font-weight:700;font-size:15px}
.sbar-val.in{color:#166534}
.sbar-val.out{color:#c2410c}
.sbar-val.net{color:var(--teal)}
.sbar-lbl{color:var(--muted)}

/* Modals */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:100;
               display:flex;align-items:center;justify-content:center;
               opacity:0;pointer-events:none;transition:opacity .2s}
.modal-overlay.open{opacity:1;pointer-events:all}
.modal{background:var(--card);border-radius:12px;width:480px;max-width:94vw;
       box-shadow:0 8px 40px rgba(0,0,0,.22);
       transform:translateY(16px) scale(.97);transition:transform .22s cubic-bezier(.34,1.3,.64,1)}
.modal-overlay.open .modal{transform:translateY(0) scale(1)}
.modal-hdr{padding:16px 20px 12px;border-bottom:1px solid var(--border);
           display:flex;align-items:center;justify-content:space-between}
.modal-hdr h3{font-size:15px;font-weight:700;color:var(--navy)}
.modal-close{background:none;border:none;font-size:18px;cursor:pointer;color:var(--muted);
             width:28px;height:28px;display:flex;align-items:center;justify-content:center;
             border-radius:4px;transition:background .12s}
.modal-close:hover{background:#f1f3f8}
.modal-body{padding:16px 20px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 14px}
.form-grid .full{grid-column:1/-1}
.fld label{display:block;font-size:11px;font-weight:600;color:var(--muted);
           margin-bottom:3px;text-transform:uppercase;letter-spacing:.4px}
.fld input,.fld select{width:100%;border:1.5px solid var(--border);border-radius:6px;
                        padding:6px 9px;font-size:13px;outline:none;
                        transition:border .15s,box-shadow .15s;background:#fff;color:var(--text)}
.fld input:focus,.fld select:focus{border-color:var(--teal2);box-shadow:0 0 0 3px rgba(68,114,196,.15)}
.fld.req label::after{content:' *';color:#dc2626}
.modal-footer{padding:12px 20px 16px;display:flex;justify-content:flex-end;gap:10px;
              border-top:1px solid var(--border)}
.btn-cancel{border:1.5px solid var(--border);border-radius:6px;padding:7px 18px;font-size:13px;
            cursor:pointer;background:var(--card);color:var(--muted);transition:all .14s}
.btn-cancel:hover{background:#f1f3f8;color:var(--text)}
.btn-submit{background:#b45309;color:#fff;border:none;border-radius:6px;padding:8px 22px;
            font-size:13px;font-weight:600;cursor:pointer;transition:background .15s}
.btn-submit:hover:not(:disabled){background:#92400e}
.btn-submit:disabled{opacity:.4;cursor:not-allowed}
.btn-submit.green{background:#166534}
.btn-submit.green:hover:not(:disabled){background:#14532d}
.remark-preview{margin-top:6px;background:#f8fafc;border:1px solid var(--border);border-radius:6px;
                padding:6px 9px;font-size:12px;color:var(--muted);min-height:32px;word-break:break-word}
.remark-preview span{color:var(--text);font-weight:500}
.modal-sub{font-size:12px;color:var(--muted);margin-bottom:14px;
           background:#fef3c7;border:1px solid #fde68a;border-radius:6px;padding:8px 10px;color:#92400e}

/* Toast */
.toast{position:fixed;top:18px;right:18px;z-index:999;padding:10px 16px;border-radius:8px;
       font-weight:500;font-size:13px;box-shadow:0 4px 18px rgba(0,0,0,.18);
       transform:translateY(-70px) scale(.95);opacity:0;
       transition:all .28s cubic-bezier(.34,1.56,.64,1);pointer-events:none}
.toast.show{transform:translateY(0) scale(1);opacity:1}
.toast.ok{background:#166534;color:#fff}
.toast.err{background:#991b1b;color:#fff}
.toast.warn{background:#b45309;color:#fff}

/* Spinner */
.spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(47,84,150,.2);
         border-top-color:var(--teal);border-radius:50%;
         animation:spin .7s linear infinite;vertical-align:middle}
.spinner.wh{border-color:rgba(255,255,255,.3);border-top-color:#fff}
@keyframes spin{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<!-- Header -->
<div class="hdr">
  <div class="hdr-left">
    <div class="hdr-title">
      <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path d="M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2z"/>
        <path d="M16 3H8l-2 4h12l-2-4z"/>
      </svg>
      Local Inventory — Canada
    </div>
    <div class="nav-tabs">
      <button class="nav-tab active" id="tab-inv"  onclick="showView('inventory')">📦 Inventory</button>
      <button class="nav-tab"        id="tab-hist" onclick="showView('history')">📋 History</button>
    </div>
    <div style="display:flex;align-items:center;gap:10px;margin-left:auto;padding-right:4px">
      <span id="user-label" style="font-size:12px;color:#94a3b8"></span>
      <a href="/logout" style="font-size:12px;color:#64748b;text-decoration:none;border:1px solid #e2e8f0;border-radius:6px;padding:4px 10px;background:#fff">Sign out</a>
    </div>
  </div>
  <div class="hdr-stats" id="hdr-inv-stats">
    <div class="stat clickable" id="badge-total" style="background:rgba(255,255,255,.18);border-color:rgba(255,255,255,.35)" onclick="setBadgeFilter('reset')"><span class="stat-dot" style="background:#fff"></span>Total<span class="stat-n" id="s-total">—</span></div>
    <div style="width:1px;height:22px;background:rgba(255,255,255,.2);margin:0 2px"></div>
    <div class="stat clickable" id="badge-s4" style="background:rgba(255,255,255,.08)" onclick="setBadgeFilter('mdl','S4')"><span class="stat-dot" style="background:#7dd3fc"></span>S4<span class="stat-n" id="s-s4">—</span></div>
    <div class="stat clickable" id="badge-v3s" style="background:rgba(255,255,255,.08)" onclick="setBadgeFilter('mdl','V3S')"><span class="stat-dot" style="background:#a5b4fc"></span>V3S<span class="stat-n" id="s-v3s">—</span></div>
    <div class="stat clickable" id="badge-v4" style="background:rgba(255,255,255,.08)" onclick="setBadgeFilter('mdl','V4')"><span class="stat-dot" style="background:#6ee7b7"></span>V4<span class="stat-n" id="s-v4">—</span></div>
  </div>
  <div id="hdr-hist-stats" style="display:none;gap:8px" class="hdr-stats">
    <div class="stat ln"><span class="stat-dot" style="background:#93c5fd"></span>IN<span class="stat-n" id="hs-in">—</span></div>
    <div class="stat rp"><span class="stat-dot" style="background:#fdba74"></span>OUT<span class="stat-n" id="hs-out">—</span></div>
    <div class="stat nw"><span class="stat-dot" style="background:#86efac"></span>Net<span class="stat-n" id="hs-net">—</span></div>
  </div>
</div>

<!-- ═══════════════════════════════════════════ INVENTORY VIEW -->
<div id="view-inventory" style="display:contents">
  <div class="toolbar">
    <div class="search">
      <svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
      <input id="inv-q" type="text" placeholder="Search model, code, customer…" oninput="applyInvFilters()">
    </div>
    <div class="filters">
      <button class="fb on"  onclick="setInvCat('all',this)">All</button>
      <button class="fb ln"  onclick="setInvCat('Like New',this)">Like New<span class="fb-cnt" id="fb-ln">—</span></button>
      <button class="fb rp"  onclick="setInvCat('Replacement',this)">Replacement<span class="fb-cnt" id="fb-rp">—</span></button>
      <button class="fb nw"  onclick="setInvCat('New',this)">New<span class="fb-cnt" id="fb-nw">—</span></button>
      <button class="fb bk"  onclick="setInvCat('booked',this)">Booked<span class="fb-cnt" id="fb-bk">—</span></button>
    </div>
    <div class="sp"></div>
    <button class="tb" onclick="selectAll()">Select All</button>
    <button class="tb" onclick="selectNone()">Clear</button>
    <button class="btn-ci" onclick="openCheckin()">
      <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path d="M12 5v14M5 12h14"/></svg>
      Check In
    </button>
  </div>

  <div class="tbl-wrap" id="inv-tbl-wrap">
    <table>
      <thead><tr>
        <th class="ns" style="width:38px"><input type="checkbox" id="hdr-cb" onchange="toggleAll(this)"></th>
        <th onclick="sortInv('model')">Model</th>
        <th onclick="sortInv('version')">Version</th>
        <th onclick="sortInv('code')">Code</th>
        <th onclick="sortInv('category')">Category</th>
        <th onclick="sortInv('rating')">Rtg</th>
        <th onclick="sortInv('date')">Inbound Date</th>
        <th onclick="sortInv('customer')">Customer</th>
        <th onclick="sortInv('order')">Order</th>
        <th onclick="sortInv('packaging')">Packaging</th>
        <th onclick="sortInv('remark')">Remark</th>
      </tr></thead>
      <tbody id="inv-tbody">
        <tr><td colspan="11" class="empty"><span class="spinner"></span>&nbsp; Loading…</td></tr>
      </tbody>
    </table>
  </div>

  <!-- Action bar -->
  <div class="ckbar">
    <div class="sel-lbl"><b id="sel-n">0</b> selected</div>
    <div class="vdiv"></div>

    <!-- No selection -->
    <div class="bar-state" id="bar-none">
      <span class="bar-hint" id="bar-hint">Select cameras to take action</span>
    </div>

    <!-- Available cameras selected -->
    <div class="bar-state" id="bar-avail" style="display:none">
      <button class="btn-bookout" id="bo-btn" onclick="openBookout()">
        <svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.2"><path d="M5 12h14M12 5l7 7-7 7"/></svg>
        Book Out
      </button>
      <div class="vdiv" id="edit-div-avail" style="display:none"></div>
      <button class="btn-edit" id="edit-btn-avail" style="display:none" onclick="openEdit()">
        <svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.2"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        Edit
      </button>
    </div>

    <!-- Booked cameras selected -->
    <div class="bar-state" id="bar-booked-act" style="display:none">
      <button class="btn-putback" id="pb-btn" onclick="doPutBack()">
        <svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
        Put Back
      </button>
      <div class="vdiv"></div>
      <button class="btn-complete" id="cp-btn" onclick="doComplete()">
        <svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path d="M5 13l4 4L19 7"/></svg>
        Complete — Ship
      </button>
      <div class="vdiv" id="edit-div-booked" style="display:none"></div>
      <button class="btn-edit" id="edit-btn-booked" style="display:none" onclick="openEdit()">
        <svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.2"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        Edit
      </button>
    </div>

    <div class="sp2"></div>
  </div>
</div>

<!-- ═══════════════════════════════════════════ HISTORY VIEW -->
<div id="view-history" style="display:none;flex-direction:column;flex:1;overflow:hidden">
  <div class="toolbar">
    <div class="search">
      <svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.2"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
      <input id="hist-q" type="text" placeholder="Search code, version, customer, order…" oninput="applyHistFilters()">
    </div>
    <div class="filters">
      <button class="fb on"  onclick="setHistDir('all',this)">All</button>
      <button class="fb in"  onclick="setHistDir('in',this)">↓ IN</button>
      <button class="fb out" onclick="setHistDir('out',this)">↑ OUT</button>
    </div>
    <div class="filters">
      <button class="fb on"  onclick="setHistVer('all',this)">All</button>
      <button class="fb"     onclick="setHistVer('4G US',this)">4G US</button>
      <button class="fb"     onclick="setHistVer('4G EU',this)">4G EU</button>
      <button class="fb"     onclick="setHistVer('WiFi',this)">WiFi</button>
    </div>
    <div class="filters">
      <button class="fb on"  onclick="setHistRsn('all',this)">All</button>
      <button class="fb"     onclick="setHistRsn('Return',this)">Return</button>
      <button class="fb"     onclick="setHistRsn('Replacement',this)">Replacement</button>
      <button class="fb"     onclick="setHistRsn('Sale',this)">Sale</button>
    </div>
    <div class="sp"></div>
    <span class="meta-txt" id="hist-count"></span>
    <button class="btn-refresh" onclick="reloadHistory()">
      <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path d="M4 4v5h5M20 20v-5h-5"/><path d="M4 9a9 9 0 0115-3M20 15a9 9 0 01-15 3"/></svg>
      Refresh
    </button>
  </div>

  <div class="tbl-wrap" id="hist-tbl-wrap">
    <table>
      <thead><tr>
        <th onclick="sortHist('model')">Model</th>
        <th onclick="sortHist('version')">Version</th>
        <th onclick="sortHist('code')">Code</th>
        <th onclick="sortHist('category')">Category</th>
        <th onclick="sortHist('rating')">Rtg</th>
        <th onclick="sortHist('date')">Date</th>
        <th onclick="sortHist('customer')">Customer</th>
        <th onclick="sortHist('order')">Order</th>
        <th class="ns">Direction</th>
        <th onclick="sortHist('reason')">Reason</th>
      </tr></thead>
      <tbody id="hist-tbody">
        <tr><td colspan="10" class="empty"><span class="spinner"></span>&nbsp; Loading…</td></tr>
      </tbody>
    </table>
  </div>

  <div class="statsbar">
    <div class="sbar-item"><span class="sbar-dot in"></span><span class="sbar-val in" id="stat-in">—</span><span class="sbar-lbl">inbound</span></div>
    <div class="vdiv"></div>
    <div class="sbar-item"><span class="sbar-dot out"></span><span class="sbar-val out" id="stat-out">—</span><span class="sbar-lbl">outbound</span></div>
    <div class="vdiv"></div>
    <div class="sbar-item"><span class="sbar-dot net"></span><span class="sbar-val net" id="stat-net">—</span><span class="sbar-lbl">net</span></div>
    <div style="flex:1"></div>
    <span class="meta-txt" id="hist-range"></span>
  </div>
</div>

<!-- ═══════════════════════════════════════ BOOK OUT MODAL -->
<div class="modal-overlay" id="bo-overlay" onclick="boOverlayClick(event)">
  <div class="modal">
    <div class="modal-hdr">
      <h3>📤 Book Out</h3>
      <button class="modal-close" onclick="closeBookout()">✕</button>
    </div>
    <div class="modal-body">
      <p class="modal-sub" id="bo-sub"></p>
      <div class="form-grid">
        <div class="fld req full"><label>Purpose</label>
          <select id="bo-purpose" onchange="boPurposeChange()">
            <option value="">— select —</option>
            <option value="Replacement">Replacement</option>
            <option value="Sale">Sale</option>
            <option value="Internal Test">Internal Test</option>
            <option value="Other">Other…</option>
          </select>
        </div>
        <div class="fld full" id="bo-other-wrap" style="display:none"><label>Specify purpose</label>
          <input id="bo-other" placeholder="Describe the purpose…">
        </div>
        <div class="fld"><label>Customer / Destination</label>
          <input id="bo-customer" placeholder="Name or location">
        </div>
        <div class="fld"><label>Order #</label>
          <input id="bo-order" placeholder="Optional">
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeBookout()">Cancel</button>
      <button class="btn-submit" id="bo-submit" onclick="doBookout()">📤 Book Out</button>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════ CHECK-IN MODAL -->
<div class="modal-overlay" id="ci-overlay" onclick="overlayClick(event)">
  <div class="modal">
    <div class="modal-hdr">
      <h3>➕ Check In Camera</h3>
      <button class="modal-close" onclick="closeCheckin()">✕</button>
    </div>
    <div class="modal-body">
      <div class="form-grid">
        <div class="fld req"><label>Model</label>
          <input id="ci-model" list="mdl-list" placeholder="S4, V4, V3S…" oninput="ciPreview()">
          <datalist id="mdl-list"><option value="S4"><option value="P2-4K"><option value="V4"><option value="V3S"></datalist>
        </div>
        <div class="fld"><label>Version</label>
          <input id="ci-version" placeholder="4G US, WiFi…" oninput="ciPreview()">
        </div>
        <div class="fld req"><label>Category</label>
          <select id="ci-category" onchange="ciPreview()">
            <option value="">— select —</option>
            <option value="Like New">Like New</option>
            <option value="New">New</option>
            <option value="Replacement">Replacement</option>
          </select>
        </div>
        <div class="fld"><label>Rating (1–10)</label>
          <input id="ci-rating" type="number" min="1" max="10" step="0.5" placeholder="9">
        </div>
        <div class="fld"><label>Returned From</label>
          <input id="ci-customer" placeholder="Customer name" oninput="ciPreview()">
        </div>
        <div class="fld"><label>Order #</label>
          <input id="ci-order" placeholder="Order number" oninput="ciPreview()">
        </div>
        <div class="fld"><label>Camera Code</label>
          <input id="ci-code" placeholder="e.g. CV6E" maxlength="4"
                 style="text-transform:uppercase;font-family:'SF Mono',monospace;letter-spacing:.6px"
                 oninput="this.value=this.value.toUpperCase();ciPreview()">
        </div>
        <div class="fld"><label>Notes</label>
          <input id="ci-notes" placeholder="Optional" oninput="ciPreview()">
        </div>
        <div class="fld"><label>Packaging</label>
          <select id="ci-packaging">
            <option value="">— select —</option>
            <option value="Neutral">Neutral</option>
            <option value="Customized">Customized</option>
          </select>
        </div>
        <div class="fld full"><label>Remark preview</label>
          <div class="remark-preview" id="ci-preview"><span>—</span></div>
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeCheckin()">Cancel</button>
      <button class="btn-submit green" id="ci-submit" onclick="doCheckin()">✓ Add to Inventory</button>
    </div>
  </div>
</div>

<!-- ═══════════════════════════════════════ EDIT MODAL -->
<div class="modal-overlay" id="ed-overlay" onclick="edOverlayClick(event)">
  <div class="modal">
    <div class="modal-hdr">
      <h3>✏️ Edit Camera</h3>
      <button class="modal-close" onclick="closeEdit()">✕</button>
    </div>
    <div class="modal-body">
      <div class="form-grid">
        <div class="fld req"><label>Model</label>
          <input id="ed-model" list="mdl-list">
        </div>
        <div class="fld"><label>Version</label>
          <input id="ed-version" placeholder="4G US, WiFi…">
        </div>
        <div class="fld req"><label>Category</label>
          <select id="ed-category">
            <option value="">— select —</option>
            <option value="Like New">Like New</option>
            <option value="New">New</option>
            <option value="Replacement">Replacement</option>
          </select>
        </div>
        <div class="fld"><label>Rating (1–10)</label>
          <input id="ed-rating" type="number" min="1" max="10" step="0.5">
        </div>
        <div class="fld"><label>Camera Code</label>
          <input id="ed-code" maxlength="4"
                 style="text-transform:uppercase;font-family:'SF Mono',monospace;letter-spacing:.6px"
                 oninput="this.value=this.value.toUpperCase()">
        </div>
        <div class="fld"><label>Inbound Date</label>
          <input id="ed-date" type="date">
        </div>
        <div class="fld"><label>Customer</label>
          <input id="ed-customer">
        </div>
        <div class="fld"><label>Order #</label>
          <input id="ed-order">
        </div>
        <div class="fld"><label>Packaging</label>
          <select id="ed-packaging">
            <option value="">— select —</option>
            <option value="Neutral">Neutral</option>
            <option value="Customized">Customized</option>
          </select>
        </div>
        <div class="fld full"><label>Remark</label>
          <input id="ed-remark">
        </div>
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn-cancel" onclick="closeEdit()">Cancel</button>
      <button class="btn-submit green" id="ed-submit" onclick="doEdit()">✓ Save Changes</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ── State ─────────────────────────────────────────────────────────────────
let invAll=[], invFiltered=[], invSel=new Set();
let invFcat='all', invFmdl='all', invScol='model', invSdir=1;

let histAll=[], histFiltered=[];
let histFdir='all', histFver='all', histFrsn='all', histScol='date', histSdir=-1;

const INV_COLS  = ['model','version','code','category','rating','date','customer','order','packaging','remark'];
const HIST_COLS = ['model','version','code','category','rating','date','customer','order','direction','reason'];

// ── Init ──────────────────────────────────────────────────────────────────
async function init(){
  loadInventory();
  try {
    const r = await fetch('/api/whoami');
    const d = await r.json();
    const lbl = document.getElementById('user-label');
    if(lbl) lbl.textContent = d.user + (d.role === 'admin' ? ' (admin)' : ' (read-only)');
    if(d.role !== 'admin'){
      // Read-only: disable and grey out write actions
      const writeEls = [
        document.querySelector('.btn-ci'),
        document.getElementById('bo-btn'),
        document.getElementById('edit-btn-avail'),
        document.getElementById('edit-btn-booked'),
        document.getElementById('pb-btn'),
        document.getElementById('cp-btn'),
      ];
      writeEls.forEach(el => {
        if(!el) return;
        el.disabled = true;
        el.style.opacity = '0.35';
        el.style.cursor = 'not-allowed';
        el.onclick = e => e.stopPropagation();
      });
    }
  } catch(e){}
}

// ── View switching ────────────────────────────────────────────────────────
function showView(v){
  document.getElementById('tab-inv').classList.toggle('active',  v==='inventory');
  document.getElementById('tab-hist').classList.toggle('active', v==='history');
  document.getElementById('view-inventory').style.display = v==='inventory' ? 'contents' : 'none';
  document.getElementById('view-history').style.display   = v==='history'   ? 'flex'     : 'none';
  document.getElementById('hdr-inv-stats').style.display  = v==='inventory' ? 'flex'     : 'none';
  document.getElementById('hdr-hist-stats').style.display = v==='history'   ? 'flex'     : 'none';
  if(v==='history' && !histAll.length) loadHistory();
}

// ══════════════════════════════════════════════ INVENTORY
async function loadInventory(){
  try{
    const d=await fetch('/api/inventory').then(r=>r.json());
    if(!d.ok) throw new Error(d.error);
    invAll=d.items; invSel.clear(); applyInvFilters(); updateInvStats();
  }catch(e){
    document.getElementById('inv-tbody').innerHTML=
      `<tr><td colspan="11" class="empty">⚠️ ${e.message}</td></tr>`;
  }
}

function updateInvStats(){
  const c = cat => invAll.filter(i=>i.category===cat).length;
  const m = mdl => invAll.filter(i=>i.model===mdl).length;
  document.getElementById('s-total').textContent = invAll.length;
  document.getElementById('s-s4').textContent  = m('S4');
  document.getElementById('s-v3s').textContent = m('V3S');
  document.getElementById('s-v4').textContent  = m('V4');
  document.getElementById('fb-ln').textContent = c('Like New');
  document.getElementById('fb-rp').textContent = c('Replacement');
  document.getElementById('fb-nw').textContent = c('New');
  document.getElementById('fb-bk').textContent = invAll.filter(i=>i.status==='booked').length;
}

function catClass(c){return{'Like New':'ln','New':'nw','Replacement':'rp'}[c]||''}

function ratingBadge(rating){
  if(rating==null||rating==='') return '';
  const r = Math.min(10, Math.max(5, Number(rating)));
  const t = (r - 5) / 5;   // 0 = rating 5 (orange), 1 = rating 10 (green)
  // bg: orange #ffedd5 → green #dcfce7
  const bgR = Math.round(255 + (220-255)*t);
  const bgG = Math.round(237 + (252-237)*t);
  const bgB = Math.round(213 + (231-213)*t);
  // text: orange #9a3412 → green #15803d
  const fgR = Math.round(154 + (21-154)*t);
  const fgG = Math.round(52  + (128-52)*t);
  const fgB = Math.round(18  + (61-18)*t);
  return `<span class="badge" style="background:rgb(${bgR},${bgG},${bgB});color:rgb(${fgR},${fgG},${fgB})">${Number(rating).toFixed(1)}</span>`;
}

function applyInvFilters(){
  const q = document.getElementById('inv-q').value.toLowerCase().trim();
  invFiltered = invAll.filter(i=>{
    const isBooked = i.status === 'booked';
    if(invFmdl !== 'all' && i.model !== invFmdl) return false;
    if(invFcat === 'booked')  return isBooked;
    if(invFcat !== 'all' && i.category !== invFcat) return false;
    if(invFcat !== 'all' && isBooked) return false;
    const srchOk = !q || [i.model,i.version,i.code,i.customer,i.order,
                           i.booked_customer,i.booked_order,i.booked_purpose]
                           .some(v=>(v||'').toLowerCase().includes(q));
    return srchOk;
  });
  sortInvItems(); renderInv();
}

function setInvCat(f,btn){
  invFcat=f;
  document.querySelectorAll('#view-inventory .toolbar .fb').forEach(b=>b.classList.remove('on'));
  btn.classList.add('on'); applyInvFilters();
}

function setBadgeFilter(type, value){
  const mdlMap = {'S4':'badge-s4','V3S':'badge-v3s','V4':'badge-v4'};

  if(type === 'reset'){
    invFcat='all'; invFmdl='all';
    ['badge-s4','badge-v3s','badge-v4'].forEach(id=>document.getElementById(id).classList.remove('active'));
    document.querySelectorAll('#view-inventory .toolbar .fb').forEach((b,i)=>{b.classList.toggle('on',i===0);});
  } else if(type === 'mdl'){
    const badgeId = mdlMap[value];
    const isActive = document.getElementById(badgeId).classList.contains('active');
    Object.values(mdlMap).forEach(id=>document.getElementById(id).classList.remove('active'));
    invFmdl = isActive ? 'all' : value;
    if(!isActive) document.getElementById(badgeId).classList.add('active');
  }
  applyInvFilters();
}

function sortInv(col){
  invSdir=invScol===col?-invSdir:1; invScol=col;
  document.querySelectorAll('#inv-tbl-wrap thead th').forEach((th,i)=>{
    th.classList.remove('sa','sd');
    if(i>0 && INV_COLS[i-1]===col) th.classList.add(invSdir===1?'sa':'sd');
  });
  sortInvItems(); renderInv();
}
function sortInvItems(){
  invFiltered.sort((a,b)=>{
    const va=a[invScol]??'', vb=b[invScol]??'';
    if(typeof va==='number'&&typeof vb==='number') return (va-vb)*invSdir;
    return String(va).localeCompare(String(vb))*invSdir;
  });
}

function renderInv(){
  const tb = document.getElementById('inv-tbody');
  if(!invFiltered.length){
    tb.innerHTML='<tr><td colspan="11" class="empty">No cameras match.</td></tr>';
    return;
  }
  tb.innerHTML = invFiltered.map(it=>{
    const s       = invSel.has(it.row);
    const booked  = it.status === 'booked';
    const cc      = catClass(it.category);
    const rtg     = ratingBadge(it.rating);
    const rowCls  = booked ? 'row-booked' : '';

    const codeTd  = booked
      ? `<span class="mono">${it.code||'—'}</span>&nbsp;<span class="badge bk">${it.booked_purpose||'Booked'}</span>`
      : `<span class="mono">${it.code||'—'}</span>`;

    const custTd  = booked
      ? `<span>${it.booked_customer||'—'}</span>`
      : (it.customer||'');
    const ordTd   = booked ? (it.booked_order||'') : (it.order||'');

    return `<tr class="clickable ${rowCls}${s?' sel':''}" onclick="toggleInvRow(${it.row})">
      <td><input type="checkbox"${s?' checked':''} onclick="event.stopPropagation();toggleInvRow(${it.row})"></td>
      <td><strong>${it.model}</strong></td>
      <td>${it.version||''}</td>
      <td>${codeTd}</td>
      <td><span class="badge ${cc}">${it.category}</span></td>
      <td class="rtg">${rtg}</td>
      <td>${it.date||''}</td>
      <td>${custTd}</td>
      <td>${ordTd}</td>
      <td>${it.packaging ? `<span class="badge ${it.packaging==='Neutral'?'oth-b':'ln'}">${it.packaging}</span>` : ''}</td>
      <td style="color:var(--muted);font-size:11.5px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${(it.remark||'').replace(/"/g,'&quot;')}">${it.remark||''}</td>
    </tr>`;
  }).join('');
  updateSelLbl();
}

function toggleInvRow(r){ invSel.has(r)?invSel.delete(r):invSel.add(r); renderInv(); }
function selectAll(){ invFiltered.forEach(i=>invSel.add(i.row)); renderInv(); }
function selectNone(){ invSel.clear(); renderInv(); }
function toggleAll(cb){ cb.checked ? selectAll() : selectNone(); }

function updateSelLbl(){
  const n = invSel.size;
  document.getElementById('sel-n').textContent = n;
  const cb = document.getElementById('hdr-cb');
  cb.indeterminate = n>0 && n<invFiltered.length;
  cb.checked = n>0 && invFiltered.length>0 && n>=invFiltered.length;

  // Determine bar state
  if(n === 0){
    setBar('none', 'Select cameras to take action');
    return;
  }
  const sel       = invAll.filter(i => invSel.has(i.row));
  const allAvail  = sel.every(i => !i.status || i.status === 'available');
  const allBooked = sel.every(i => i.status === 'booked');

  const single = n === 1;
  ['avail','booked'].forEach(t=>{
    document.getElementById(`edit-btn-${t}`).style.display  = single ? 'flex'  : 'none';
    document.getElementById(`edit-div-${t}`).style.display  = single ? 'block' : 'none';
  });
  if(allAvail)       setBar('avail');
  else if(allBooked) setBar('booked-act');
  else               setBar('none', `⚠️ Mixed selection — choose only available or only booked cameras`);
}

function setBar(state, msg=''){
  ['none','avail','booked-act'].forEach(s=>{
    document.getElementById(`bar-${s}`).style.display = s===state ? 'flex' : 'none';
  });
  if(state==='none') document.getElementById('bar-hint').textContent = msg || 'Select cameras to take action';
}

// ══════════════════════════════════════════════ BOOK OUT
function openBookout(){
  const n = invSel.size;
  document.getElementById('bo-sub').textContent =
    `Booking out ${n} camera${n!==1?'s':''} — they will be held and removed from available stock.`;
  document.getElementById('bo-purpose').value = '';
  document.getElementById('bo-other').value = '';
  document.getElementById('bo-other-wrap').style.display = 'none';
  document.getElementById('bo-customer').value = '';
  document.getElementById('bo-order').value = '';
  document.getElementById('bo-submit').disabled = false;
  document.getElementById('bo-submit').textContent = '📤 Book Out';
  document.getElementById('bo-overlay').classList.add('open');
  setTimeout(()=>document.getElementById('bo-purpose').focus(), 220);
}
function closeBookout(){ document.getElementById('bo-overlay').classList.remove('open'); }
function boOverlayClick(e){ if(e.target===document.getElementById('bo-overlay')) closeBookout(); }

function boPurposeChange(){
  const isOther = document.getElementById('bo-purpose').value === 'Other';
  document.getElementById('bo-other-wrap').style.display = isOther ? 'block' : 'none';
  if(isOther) setTimeout(()=>document.getElementById('bo-other').focus(), 50);
}

async function doBookout(){
  const sel = document.getElementById('bo-purpose').value;
  if(!sel){ shakeField('bo-purpose'); toast('Purpose is required.','err'); return; }
  const purpose = sel === 'Other'
    ? document.getElementById('bo-other').value.trim()
    : sel;
  if(!purpose){ shakeField('bo-other'); toast('Please specify the purpose.','err'); return; }
  const btn = document.getElementById('bo-submit');
  btn.disabled=true; btn.innerHTML='<span class="spinner wh"></span>&nbsp; Saving…';
  try{
    const d = await fetch('/api/bookout',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        rows:[...invSel], purpose,
        customer: document.getElementById('bo-customer').value.trim(),
        order:    document.getElementById('bo-order').value.trim(),
      })}).then(r=>r.json());
    if(!d.ok) throw new Error(d.error);
    closeBookout();
    toast(`📤 ${d.count} camera${d.count!==1?'s':''} booked out — ${purpose}.`, 'warn');
    await loadInventory();
  }catch(e){ toast('⚠️ '+e.message,'err'); btn.disabled=false; btn.textContent='📤 Book Out'; }
}

// ══════════════════════════════════════════════ PUT BACK
async function doPutBack(){
  const n   = invSel.size;
  const btn = document.getElementById('pb-btn');
  btn.disabled=true; btn.innerHTML='<span class="spinner"></span>&nbsp; Restoring…';
  try{
    const d = await fetch('/api/putback',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({rows:[...invSel]})}).then(r=>r.json());
    if(!d.ok) throw new Error(d.error);
    toast(`↩ ${d.count} camera${d.count!==1?'s':''} returned to inventory.`, 'ok');
    await loadInventory();
  }catch(e){ toast('⚠️ '+e.message,'err'); }
  finally{ btn.disabled=false; btn.innerHTML='<svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.2"><path d="M19 12H5M12 19l-7-7 7-7"/></svg> Put Back'; }
}

// ══════════════════════════════════════════════ COMPLETE
async function doComplete(){
  const n   = invSel.size;
  const btn = document.getElementById('cp-btn');
  btn.disabled=true; btn.innerHTML='<span class="spinner wh"></span>&nbsp; Completing…';
  try{
    const d = await fetch('/api/complete',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({rows:[...invSel]})}).then(r=>r.json());
    if(!d.ok) throw new Error(d.error);
    toast(`✓ ${d.count} camera${d.count!==1?'s':''} shipped — history updated.`, 'ok');
    histAll=[];
    await loadInventory();
  }catch(e){ toast('⚠️ '+e.message,'err'); }
  finally{ btn.disabled=false; btn.innerHTML='<svg width="13" height="13" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2.5"><path d="M5 13l4 4L19 7"/></svg> Complete — Ship'; }
}

// ══════════════════════════════════════════════ CHECK-IN MODAL
function openCheckin(){
  ['ci-model','ci-version','ci-rating','ci-customer','ci-order','ci-code','ci-notes']
    .forEach(id=>document.getElementById(id).value='');
  document.getElementById('ci-category').value='';
  document.getElementById('ci-packaging').value='';
  document.getElementById('ci-preview').innerHTML='<span>—</span>';
  document.getElementById('ci-submit').disabled=false;
  document.getElementById('ci-submit').textContent='✓ Add to Inventory';
  document.getElementById('ci-overlay').classList.add('open');
  setTimeout(()=>document.getElementById('ci-model').focus(),220);
}
function closeCheckin(){ document.getElementById('ci-overlay').classList.remove('open'); }
function overlayClick(e){ if(e.target===document.getElementById('ci-overlay')) closeCheckin(); }
document.addEventListener('keydown',e=>{ if(e.key==='Escape'){ closeCheckin(); closeBookout(); closeEdit(); } });

// ══════════════════════════════════════════════ EDIT
let editItemId = null;

function openEdit(){
  const sel = invAll.filter(i=>invSel.has(i.row));
  if(sel.length !== 1){ toast('Select exactly one camera to edit.','warn'); return; }
  const it = sel[0];
  editItemId = it.row;
  document.getElementById('ed-model').value    = it.model    || '';
  document.getElementById('ed-version').value  = it.version  || '';
  document.getElementById('ed-category').value = it.category || '';
  document.getElementById('ed-rating').value   = it.rating   != null ? it.rating : '';
  document.getElementById('ed-code').value     = it.code     || '';
  document.getElementById('ed-date').value     = it.date     || '';
  document.getElementById('ed-customer').value = it.customer || '';
  document.getElementById('ed-order').value    = it.order    || '';
  document.getElementById('ed-packaging').value = it.packaging || '';
  document.getElementById('ed-remark').value   = it.remark   || '';
  document.getElementById('ed-submit').disabled   = false;
  document.getElementById('ed-submit').textContent = '✓ Save Changes';
  document.getElementById('ed-overlay').classList.add('open');
  setTimeout(()=>document.getElementById('ed-model').focus(), 220);
}
function closeEdit(){ document.getElementById('ed-overlay').classList.remove('open'); editItemId=null; }
function edOverlayClick(e){ if(e.target===document.getElementById('ed-overlay')) closeEdit(); }

async function doEdit(){
  const model = document.getElementById('ed-model').value.trim();
  const cat   = document.getElementById('ed-category').value;
  if(!model){ shakeField('ed-model');    toast('Model is required.','err'); return; }
  if(!cat)  { shakeField('ed-category'); toast('Category is required.','err'); return; }
  const btn = document.getElementById('ed-submit');
  btn.disabled = true; btn.innerHTML = '<span class="spinner wh"></span>&nbsp; Saving…';
  try{
    const d = await fetch('/api/update',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({
        id:       editItemId,
        model,
        version:  document.getElementById('ed-version').value.trim(),
        category: cat,
        rating:   document.getElementById('ed-rating').value.trim(),
        customer: document.getElementById('ed-customer').value.trim(),
        order:    document.getElementById('ed-order').value.trim(),
        code:     document.getElementById('ed-code').value.trim().toUpperCase(),
        date:     document.getElementById('ed-date').value.trim(),
        packaging: document.getElementById('ed-packaging').value,
        remark:    document.getElementById('ed-remark').value.trim(),
      })
    }).then(r=>r.json());
    if(!d.ok) throw new Error(d.error);
    closeEdit();
    toast(`✓ Camera updated.`, 'ok');
    await loadInventory();
  }catch(e){
    toast('⚠️ '+e.message,'err');
    btn.disabled=false; btn.textContent='✓ Save Changes';
  }
}

function ciPreview(){
  const cu=document.getElementById('ci-customer').value.trim();
  const or=document.getElementById('ci-order').value.trim();
  const co=document.getElementById('ci-code').value.trim().toUpperCase();
  const no=document.getElementById('ci-notes').value.trim();
  let p=[];
  if(cu||or){ let b='Return from'; if(cu) b+=' '+cu; if(or) b+=` (${or})`; p.push(b); }
  if(co) p.push(co);
  if(no) p.push(no);
  document.getElementById('ci-preview').innerHTML=
    `<span>${p.length?p.join(', '):'Check-in'}</span>`;
}
async function doCheckin(){
  const model=document.getElementById('ci-model').value.trim();
  const cat  =document.getElementById('ci-category').value;
  if(!model){ shakeField('ci-model');    toast('Model is required.','err'); return; }
  if(!cat)  { shakeField('ci-category'); toast('Category is required.','err'); return; }
  const btn=document.getElementById('ci-submit');
  btn.disabled=true; btn.innerHTML='<span class="spinner wh"></span>&nbsp; Saving…';
  try{
    const d=await fetch('/api/checkin',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        model, version:document.getElementById('ci-version').value.trim(), category:cat,
        rating:document.getElementById('ci-rating').value.trim(),
        customer:document.getElementById('ci-customer').value.trim(),
        order:document.getElementById('ci-order').value.trim(),
        code:document.getElementById('ci-code').value.trim().toUpperCase(),
        notes:document.getElementById('ci-notes').value.trim(),
        packaging:document.getElementById('ci-packaging').value,
      })}).then(r=>r.json());
    if(!d.ok) throw new Error(d.error);
    closeCheckin(); toast(`✓ ${model} checked in.`,'ok');
    histAll=[];
    await loadInventory();
  }catch(e){ toast('⚠️ '+e.message,'err'); btn.disabled=false; btn.textContent='✓ Add to Inventory'; }
}

function shakeField(id){
  const el=document.getElementById(id);
  el.style.borderColor='#dc2626'; el.style.boxShadow='0 0 0 3px rgba(220,38,38,.2)';
  setTimeout(()=>{ el.style.borderColor=''; el.style.boxShadow=''; },1600);
}

// ══════════════════════════════════════════════ HISTORY
async function loadHistory(){
  document.getElementById('hist-tbody').innerHTML=
    '<tr><td colspan="10" class="empty"><span class="spinner"></span>&nbsp; Loading…</td></tr>';
  try{
    const d=await fetch('/api/history').then(r=>r.json());
    if(!d.ok) throw new Error(d.error);
    histAll=d.items; applyHistFilters(); updateHistStats(histAll);
  }catch(e){
    document.getElementById('hist-tbody').innerHTML=
      `<tr><td colspan="10" class="empty">⚠️ ${e.message}</td></tr>`;
  }
}
async function reloadHistory(){ histAll=[]; await loadHistory(); }

function applyHistFilters(){
  const q=document.getElementById('hist-q').value.toLowerCase().trim();
  histFiltered=histAll.filter(i=>{
    const dirOk = histFdir==='all' || i.direction===histFdir;
    const verOk = histFver==='all' || i.version===histFver;
    const rsnOk = histFrsn==='all' || i.reason===histFrsn;
    const srchOk= !q || [i.code,i.version,i.customer,i.order,i.reason]
                          .some(v=>(v||'').toLowerCase().includes(q));
    return dirOk && verOk && rsnOk && srchOk;
  });
  sortHistItems(); renderHist(); updateHistStats(histFiltered);
  document.getElementById('hist-count').textContent=`${histFiltered.length} records`;
}
function _setHistFilt(btn,n){
  document.querySelectorAll(
    `#view-history .toolbar .filters:nth-of-type(${n}) .fb`
  ).forEach(b=>b.classList.remove('on'));
  btn.classList.add('on');
}
function setHistDir(f,btn){ histFdir=f; _setHistFilt(btn,1); applyHistFilters(); }
function setHistVer(v,btn){ histFver=v; _setHistFilt(btn,2); applyHistFilters(); }
function setHistRsn(r,btn){ histFrsn=r; _setHistFilt(btn,3); applyHistFilters(); }

function sortHist(col){
  histSdir=histScol===col?-histSdir:1; histScol=col;
  document.querySelectorAll('#hist-tbl-wrap thead th').forEach((th,i)=>{
    th.classList.remove('sa','sd');
    if(HIST_COLS[i]===col) th.classList.add(histSdir===1?'sa':'sd');
  });
  sortHistItems(); renderHist();
}
function sortHistItems(){
  histFiltered.sort((a,b)=>{
    const va=a[histScol]??'', vb=b[histScol]??'';
    if(typeof va==='number'&&typeof vb==='number') return (va-vb)*histSdir;
    return String(va).localeCompare(String(vb))*histSdir;
  });
}
function dirBadge(d){
  return d==='in'
    ? '<span class="badge in-b">↓ IN</span>'
    : '<span class="badge out-b">↑ OUT</span>';
}
function rsnBadge(r){
  const m={'Return':'ret-b','Replacement':'rep-b','Sale':'sal-b'};
  return `<span class="badge ${m[r]||'oth-b'}">${r||'—'}</span>`;
}
function renderHist(){
  const tb=document.getElementById('hist-tbody');
  if(!histFiltered.length){
    tb.innerHTML='<tr><td colspan="10" class="empty">No records match this filter.</td></tr>';
    return;
  }
  tb.innerHTML=histFiltered.map(it=>{
    const cc=catClass(it.category);
    const rtg=it.rating!=null?Number(it.rating).toFixed(1):'';
    const cls=it.direction==='in'?'row-in':'row-out';
    return `<tr class="${cls}">
      <td><strong>${it.model}</strong></td>
      <td>${it.version||''}</td>
      <td class="mono">${it.code||'—'}</td>
      <td><span class="badge ${cc}">${it.category||'—'}</span></td>
      <td class="rtg">${rtg}</td>
      <td>${it.date||''}</td>
      <td>${it.customer||''}</td>
      <td>${it.order||''}</td>
      <td>${dirBadge(it.direction)}</td>
      <td>${rsnBadge(it.reason)}</td>
    </tr>`;
  }).join('');
}
function updateHistStats(items){
  const inC=items.filter(i=>i.direction==='in').length;
  const outC=items.filter(i=>i.direction==='out').length;
  const net=inC-outC;
  document.getElementById('stat-in').textContent  = inC;
  document.getElementById('stat-out').textContent = outC;
  document.getElementById('stat-net').textContent = (net>0?'+':'')+net;
  document.getElementById('hs-in').textContent    = inC;
  document.getElementById('hs-out').textContent   = outC;
  document.getElementById('hs-net').textContent   = (net>0?'+':'')+net;
  if(items.length){
    const dates=items.map(i=>i.date).filter(Boolean).sort();
    document.getElementById('hist-range').textContent=`${dates[0]}  →  ${dates[dates.length-1]}`;
  } else {
    document.getElementById('hist-range').textContent='';
  }
}

// ── Toast ─────────────────────────────────────────────────────────────────
function toast(msg,type='ok'){
  const t=document.getElementById('toast');
  t.textContent=msg; t.className=`toast ${type}`;
  setTimeout(()=>t.classList.add('show'),10);
  setTimeout(()=>t.classList.remove('show'),3800);
}

init();
</script>
</body>
</html>"""

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    if not os.path.exists(DB_FILE):
        print(f"\n  ⚠️  Database not found: {DB_FILE}\n")
    else:
        init_db()   # ensure schema is current (adds booking columns if upgrading)

    def _open():
        import time; time.sleep(0.9)
        webbrowser.open(f'http://localhost:{PORT}')

    threading.Thread(target=_open, daemon=True).start()
    print(f"\n  ✓  Inventory Portal  →  http://localhost:{PORT}")
    print(f"  ✓  Database: {DB_FILE}")
    print(f"\n  Press Ctrl+C to stop.\n")
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
