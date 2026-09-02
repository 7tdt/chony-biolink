"""
chony.info — guns.lol-style biolink site & admin panel
Run: python3 chony.py
Default port: 8080 (change with PORT env var)
Admin password: sigma123 (change with CHONY_PASSWORD env var)
"""
import os, json, time, secrets, requests, re, threading
from flask import Flask, request, jsonify, send_from_directory, make_response, redirect, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

try:
    import websocket
except ImportError:
    websocket = None

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(ROOT, "chony_data.json")
UPLOAD_DIR = os.path.join(ROOT, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)


def load_env_file(path):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except OSError:
        pass


load_env_file(os.path.join(ROOT, ".env"))
PASSWORD = os.environ.get("CHONY_PASSWORD", "sigma123")
PORT = int(os.environ.get("PORT", "8080"))
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()

LOG_WEBHOOK = os.environ.get("CHONY_LOG_WEBHOOK", "").strip()
LOG_DEDUPE_WINDOW = int(os.environ.get("CHONY_LOG_WINDOW", "900"))
_log_seen = {}
_log_seen_lock = threading.Lock()
_geo_cache = {}
_geo_lock = threading.Lock()


PRESENCE_CACHE = {}
PRESENCE_LOCK = threading.Lock()


def _update_presence(user_id, status, activities):
    with PRESENCE_LOCK:
        custom_status = None
        spotify = None
        for act in activities:
            act_type = act.get("type")
            act_name = act.get("name")
            if act_type == 4 or act_name == "Custom Status":
                st_text = act.get("state") or ""
                em = act.get("emoji") or {}
                em_name = em.get("name") or ""
                em_id = em.get("id")
                em_url = None
                if em_id:
                    em_ext = "gif" if em.get("animated") else "png"
                    em_url = f"https://cdn.discordapp.com/emojis/{em_id}.{em_ext}"
                custom_status = {
                    "text": st_text,
                    "emoji_name": em_name if not em_id else "",
                    "emoji_url": em_url
                }
            elif act_name == "Spotify" or act_type == 2:
                assets = act.get("assets") or {}
                large_img = assets.get("large_image") or ""
                album_art = ""
                if large_img.startswith("spotify:"):
                    album_art = f"https://i.scdn.co/image/{large_img.split(':', 1)[1]}"
                spotify = {
                    "song": act.get("details") or "",
                    "artist": act.get("state") or "",
                    "album": assets.get("large_text") or "",
                    "album_art_url": album_art
                }
        PRESENCE_CACHE[str(user_id)] = {
            "status": status or "offline",
            "custom_status": custom_status,
            "spotify": spotify,
            "updated_at": time.time()
        }


def start_discord_gateway():
    if not websocket or not DISCORD_BOT_TOKEN:
        print("[*] Discord Gateway presence disabled (missing websocket or token)")
        return

    def run_gateway():
        while True:
            try:
                def on_message(ws, msg):
                    try:
                        d = json.loads(msg)
                        op = d.get("op")
                        t = d.get("t")
                        if op == 10:
                            interval = d["d"]["heartbeat_interval"] / 1000.0
                            def beat():
                                while getattr(ws, "keep_running", True):
                                    time.sleep(interval)
                                    try:
                                        ws.send(json.dumps({"op": 1, "d": None}))
                                    except Exception:
                                        break
                            threading.Thread(target=beat, daemon=True).start()
                            ws.send(json.dumps({
                                "op": 2,
                                "d": {
                                    "token": DISCORD_BOT_TOKEN,
                                    "intents": 1 | 2 | 256,
                                    "properties": {"os": "linux", "browser": "chony", "device": "chony"}
                                }
                            }))
                        elif t == "READY":
                            print("[*] Discord Gateway Connected & Active for Real-time Presence")
                        elif t == "PRESENCE_UPDATE":
                            p = d.get("d") or {}
                            uid = (p.get("user") or {}).get("id")
                            if uid:
                                _update_presence(uid, p.get("status"), p.get("activities", []))
                        elif t == "GUILD_CREATE":
                            for p in d.get("d", {}).get("presences", []):
                                uid = (p.get("user") or {}).get("id")
                                if uid:
                                    _update_presence(uid, p.get("status"), p.get("activities", []))
                    except Exception as e:
                        pass

                ws = websocket.WebSocketApp(
                    "wss://gateway.discord.gg/?v=10&encoding=json",
                    on_message=on_message,
                    on_error=lambda ws, e: None,
                    on_close=lambda ws, s, m: None
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as ex:
                time.sleep(5)
            time.sleep(3)

    threading.Thread(target=run_gateway, daemon=True).start()


start_discord_gateway()


def _client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.headers.get("X-Real-IP") or request.remote_addr or "?"


def _geo_lookup(ip):
    if not ip or ip in ("?", "127.0.0.1", "::1") or ip.startswith(("10.", "192.168.", "172.")):
        return {"country": "Local", "city": "—", "isp": "private"}
    now = time.time()
    with _geo_lock:
        cached = _geo_cache.get(ip)
        if cached and now - cached[0] < 21600:
            return cached[1]
    try:
        r = requests.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "status,country,countryCode,regionName,city,isp,org,as,proxy,hosting,mobile"},
            timeout=2,
        )
        if r.status_code == 200:
            data = r.json() or {}
            with _geo_lock:
                _geo_cache[ip] = (now, data)
            return data
    except Exception:
        pass
    return {}


def _send_visitor_log(ip, ua, path, referer):
    try:
        if not LOG_WEBHOOK:
            return
        geo = _geo_lookup(ip)
        country_flag = ""
        cc = geo.get("countryCode")
        if cc and len(cc) == 2:
            country_flag = "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in cc.upper())
        loc = " · ".join(filter(None, [
            f"{country_flag} {geo.get('country', '?')}".strip(),
            geo.get("regionName"),
            geo.get("city"),
        ]))
        embed = {
            "title": "👁️ Biolink Visit — chony.info",
            "color": 0xD4B896,
            "fields": [
                {"name": "IP", "value": f"`{ip}`", "inline": True},
                {"name": "Path", "value": f"`{path}`", "inline": True},
                {"name": "Location", "value": loc or "unknown", "inline": False},
                {"name": "ISP", "value": geo.get("isp") or geo.get("org") or "unknown", "inline": True},
                {"name": "User-Agent", "value": f"```{(ua or '?')[:250]}```", "inline": False},
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if referer:
            embed["fields"].append({"name": "Referer", "value": referer[:200], "inline": False})
        requests.post(LOG_WEBHOOK, json={"embeds": [embed]}, timeout=5)
    except Exception:
        pass


def log_visitor(path):
    if not LOG_WEBHOOK:
        return
    ip = _client_ip()
    ua = request.headers.get("User-Agent", "")
    ref = request.headers.get("Referer", "")
    now = time.time()
    with _log_seen_lock:
        last = _log_seen.get(ip, 0)
        if now - last < LOG_DEDUPE_WINDOW:
            return
        _log_seen[ip] = now
    threading.Thread(target=_send_visitor_log, args=(ip, ua, path, ref), daemon=True).start()


ALLOWED_EXT = {
    "image": {"png", "jpg", "jpeg", "gif", "webp", "avif", "svg", "ico", "cur"},
    "video": {"mp4", "webm", "mov"},
    "audio": {"mp3", "wav", "ogg", "m4a", "flac"},
    "font":  {"ttf", "otf", "woff", "woff2"},
}
ALL_EXT = set().union(*ALLOWED_EXT.values())
BLOCKED_SCRIPT_EXT = {"lua", "luau", "sh", "exe", "bat", "py", "php"}


def _file_ext(name):
    clean = secure_filename(str(name or ""))
    return clean.rsplit(".", 1)[-1].lower() if "." in clean else ""


DEFAULT_DATA = {
    "discord_id": "1373371003425783951",
    "display_name": "chony calamri!",
    "custom_handle": "matchaaas.",
    "custom_avatar_url": "https://cdn.discordapp.com/avatars/1373371003425783951/a9afded18db5d37d1854cad65ae7b775.png?size=512",
    "use_discord_status": True,
    "custom_status": "",
    "status_emoji": "",
    "bio": "developer & creator\nwelcome to my biolink",
    "music_url": "",
    "music_title": "",
    "music_autoplay": True,
    "music_volume": 80,
    "background_url": "",
    "background_opacity": 100,
    "bg_color": "#08080a",
    "card_bg_color": "",
    "theme_preset": "custom",
    "accent_color": "#d4b896",
    "font_family": "Inter",
    "custom_font_url": "",
    "custom_font_name": "",
    "custom_cursor_url": "",
    "cursor_size": 32,
    "hide_system_cursor": True,
    "links": [
        {"label": "Discord Server", "url": "https://discord.gg/tCTFgPVcfq", "icon": "discord"},
        {"label": "GitHub", "url": "https://github.com", "icon": "github"},
        {"label": "YouTube", "url": "https://youtube.com", "icon": "youtube"}
    ],
    "view_count": 1337,
    "effects": {
        "typing_animation": True,
        "particles": True,
        "sakura_leaves": False,
        "tilt_card": True,
        "cursor_glow": True,
        "blur_backdrop": True,
        "cursor_trail": False
    },
    "badges": [
        {"name": "Verified", "icon": "check-badge", "color": "#d4b896"},
        {"name": "Owner", "icon": "crown", "color": "#f39c12"},
        {"name": "Developer", "icon": "code", "color": "#3498db"}
    ],
    "bubble_animation_bundle": "classic",
    "runner_transition": True,
    "presets": []
}

PRESET_FIELDS = [
    "discord_id", "display_name", "custom_handle", "custom_avatar_url",
    "use_discord_status", "custom_status", "status_emoji", "bio",
    "music_url", "music_title", "music_autoplay", "music_volume",
    "background_url", "background_opacity", "bg_color", "card_bg_color", "theme_preset",
    "accent_color", "font_family", "custom_font_url", "custom_font_name",
    "custom_cursor_url", "cursor_size", "hide_system_cursor",
    "bubble_animation_bundle", "runner_transition",
    "links", "effects", "badges"
]

VALID_TOKENS = set()


def load_data():
    if not os.path.exists(DATA_FILE):
        save_data(DEFAULT_DATA)
        return dict(DEFAULT_DATA)
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        for k, v in DEFAULT_DATA.items():
            if k not in d:
                d[k] = v
        return d
    except Exception:
        return dict(DEFAULT_DATA)


def save_data(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)


def is_admin(req):
    tok = req.cookies.get("chony_admin") or req.headers.get("X-Chony-Token")
    return tok in VALID_TOKENS


app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 1024 * 1024
CORS(app, supports_credentials=True)


@app.after_request
def set_security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return resp


def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.errorhandler(413)
def too_large(e):
    return jsonify({"ok": False, "error": f"file too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)"}), 413


@app.route("/")
def index():
    host = request.host.lower().split(":")[0]
    if host.startswith("admin.") or host.startswith("panel."):
        return _no_cache(make_response(send_from_directory(ROOT, "chony_panel.html")))
    log_visitor("/")
    return _no_cache(make_response(send_from_directory(ROOT, "chony.html")))


@app.route("/panel")
@app.route("/admin")
def panel():
    return _no_cache(make_response(send_from_directory(ROOT, "chony_panel.html")))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "chony.info", "uptime": time.time()})


@app.route("/discord")
def discord_invite():
    return redirect("https://discord.gg/tCTFgPVcfq", code=302)


@app.route("/favicon.ico")
def favicon():
    return ('', 204)
    return redirect("https://cdn.discordapp.com/avatars/1373371003425783951/a9afded18db5d37d1854cad65ae7b775.png?size=64", code=302)


@app.route("/uploads/<path:fname>")
def serve_upload(fname):
    if _file_ext(fname) in BLOCKED_SCRIPT_EXT:
        return "Not found", 404
    resp = send_from_directory(UPLOAD_DIR, fname)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Range"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, HEAD"
    return response


@app.route("/api/upload", methods=["POST"])
def upload_file():
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "no file"}), 400
    f = request.files["file"]
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "empty filename"}), 400

    f.stream.seek(0, 2)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > MAX_UPLOAD_BYTES:
        return jsonify({"ok": False, "error": f"file too large (max {MAX_UPLOAD_BYTES // 1024 // 1024} MB)"}), 400

    raw_name = secure_filename(f.filename)
    ext = _file_ext(raw_name)
    if ext in BLOCKED_SCRIPT_EXT:
        return jsonify({"ok": False, "error": "executable extensions not allowed"}), 400
    if ext not in ALL_EXT:
        return jsonify({"ok": False, "error": f"extension .{ext} not allowed"}), 400

    safe_base = re.sub(r"[^a-zA-Z0-9._-]", "_", raw_name.rsplit(".", 1)[0])[:60] or "file"
    final_name = f"{int(time.time())}_{secrets.token_hex(4)}_{safe_base}.{ext}"
    dest = os.path.join(UPLOAD_DIR, final_name)
    f.save(dest)

    kind = next((k for k, exts in ALLOWED_EXT.items() if ext in exts), "other")
    return jsonify({
        "ok": True,
        "url": f"/uploads/{final_name}",
        "filename": final_name,
        "kind": kind,
        "ext": ext,
        "size": size,
    })


@app.route("/api/uploads", methods=["GET"])
def list_uploads():
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    files = []
    try:
        for fname in os.listdir(UPLOAD_DIR):
            fpath = os.path.join(UPLOAD_DIR, fname)
            if os.path.isfile(fpath) and not fname.startswith("."):
                ext = _file_ext(fname)
                kind = next((k for k, exts in ALLOWED_EXT.items() if ext in exts), "other")
                stat = os.stat(fpath)
                files.append({
                    "filename": fname,
                    "url": f"/uploads/{fname}",
                    "size": stat.st_size,
                    "kind": kind,
                    "ext": ext,
                    "created_at": int(stat.st_mtime)
                })
        files.sort(key=lambda x: x["created_at"], reverse=True)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "files": files})


@app.route("/api/uploads/<path:fname>", methods=["DELETE"])
def delete_upload(fname):
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    safe = secure_filename(os.path.basename(fname))
    fpath = os.path.join(UPLOAD_DIR, safe)
    if os.path.exists(fpath) and os.path.isfile(fpath):
        try:
            os.remove(fpath)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": False, "error": "file not found"}), 404


def _extract_subdomain(host):
    if not host:
        return ""
    host_clean = host.split(":")[0].lower().strip()
    if host_clean in ["localhost", "127.0.0.1", "18.116.73.24"] or host_clean.replace(".", "").isdigit():
        return ""
    parts = host_clean.split(".")
    if len(parts) >= 3:
        sub = parts[0]
        if sub not in ["www", "admin", "api", "panel"]:
            return sub
    return ""


def _get_preset_for_request(d, req):
    param_slug = req.args.get("preset", "").strip().lower()
    subdomain = _extract_subdomain(req.host)
    target_slug = param_slug or subdomain
    
    if not target_slug or target_slug in ["chony", "main", "default", "root"]:
        return None
        
    presets = d.get("presets", [])
    for p in presets:
        p_slug = (p.get("slug") or p.get("name") or "").lower().strip().replace(" ", "-")
        p_name = (p.get("name") or "").lower().strip().replace(" ", "-")
        p_id = (p.get("id") or "").lower()
        if target_slug in [p_slug, p_name, p_id]:
            return p
    return None


@app.route("/api/profile", methods=["GET"])
def get_profile():
    d = load_data()
    preset = _get_preset_for_request(d, request)
    source = dict(d)
    if preset and isinstance(preset.get("snapshot"), dict):
        source.update(preset["snapshot"])
        source["active_preset_name"] = preset.get("name")
        source["active_preset_slug"] = preset.get("slug") or preset.get("name")

    return jsonify({
        "discord_id": source.get("discord_id", "1373371003425783951"),
        "display_name": source.get("display_name", "chony salami!"),
        "custom_handle": source.get("custom_handle", "matchaaas."),
        "custom_avatar_url": source.get("custom_avatar_url", ""),
        "use_discord_status": source.get("use_discord_status", True),
        "custom_status": source.get("custom_status", ""),
        "status_emoji": source.get("status_emoji", ""),
        "bubble_animation_bundle": source.get("bubble_animation_bundle", "classic"),
        "runner_transition": source.get("runner_transition", True),
        "bio": source.get("bio", ""),
        "music_url": source.get("music_url", ""),
        "music_title": source.get("music_title", ""),
        "music_autoplay": source.get("music_autoplay", True),
        "music_volume": source.get("music_volume", 80),
        "background_url": source.get("background_url", ""),
        "background_opacity": source.get("background_opacity", 100),
        "bg_color": source.get("bg_color", "#08080a"),
        "card_bg_color": source.get("card_bg_color", ""),
        "theme_preset": source.get("theme_preset", "custom"),
        "accent_color": source.get("accent_color", "#d4b896"),
        "font_family": source.get("font_family", "Inter"),
        "custom_font_url": source.get("custom_font_url", ""),
        "custom_font_name": source.get("custom_font_name", ""),
        "custom_cursor_url": source.get("custom_cursor_url", ""),
        "cursor_size": source.get("cursor_size", 32),
        "hide_system_cursor": source.get("hide_system_cursor", True),
        "links": source.get("links", []),
        "view_count": d.get("view_count", 0),
        "effects": source.get("effects", {}),
        "badges": source.get("badges", []),
        "active_preset_name": source.get("active_preset_name"),
        "active_preset_slug": source.get("active_preset_slug")
    })


@app.route("/api/profile", methods=["POST"])
def update_profile():
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    d = load_data()
    allowed = ["display_name", "custom_handle", "custom_avatar_url", "use_discord_status", "custom_status", "status_emoji",
               "bubble_animation_bundle", "runner_transition",
               "bio", "music_url", "music_title", "music_autoplay", "music_volume",
               "background_url", "background_opacity", "bg_color", "card_bg_color", "theme_preset", "accent_color", "font_family",
               "custom_font_url", "custom_font_name", "custom_cursor_url", "cursor_size",
               "hide_system_cursor", "links", "effects", "badges", "discord_id"]
    for k in allowed:
        if k in body:
            d[k] = body[k]
    save_data(d)
    return jsonify({"ok": True})


@app.route("/api/login", methods=["POST"])
@app.route("/api/auth", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    pwd = body.get("password", "")
    d = load_data()
    real = d.get("admin_password", "sigma123")
    if pwd == real:
        tok = secrets.token_urlsafe(32)
        VALID_TOKENS.add(tok)
        resp = make_response(jsonify({"ok": True}))
        resp.set_cookie("chony_admin", tok, max_age=86400 * 30, httponly=True, samesite="Lax")
        return resp
    return jsonify({"ok": False, "error": "incorrect password"}), 401


@app.route("/api/check_auth", methods=["GET"])
def check_auth():
    if is_admin(request):
        return jsonify({"ok": True})
    return jsonify({"ok": False}), 401


@app.route("/api/logout", methods=["POST", "GET"])
def logout():
    tok = request.cookies.get("chony_admin")
    if tok and tok in VALID_TOKENS:
        VALID_TOKENS.discard(tok)
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie("chony_admin")
    return resp


@app.route("/api/view", methods=["POST"])
def increment_view():
    d = load_data()
    d["view_count"] = d.get("view_count", 0) + 1
    save_data(d)
    return jsonify({"ok": True, "view_count": d["view_count"]})


def _snapshot_from(d):
    return {k: d.get(k) for k in PRESET_FIELDS}


def _find_preset(presets, pid):
    for i, p in enumerate(presets):
        if p.get("id") == pid:
            return i, p
    return -1, None


@app.route("/api/presets", methods=["GET"])
def list_presets():
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    d = load_data()
    return jsonify({"ok": True, "presets": d.get("presets", [])})


@app.route("/api/presets", methods=["POST"])
def create_preset():
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()[:40]
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    
    raw_slug = (body.get("slug") or name).lower().strip().replace(" ", "-")
    slug = re.sub(r'[^a-z0-9_-]', '', raw_slug)[:30] or "preset"

    d = load_data()
    presets = d.get("presets", [])
    if len(presets) >= 50:
        return jsonify({"ok": False, "error": "too many presets (max 50)"}), 400
    snapshot = body.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = _snapshot_from(d)
    else:
        filtered = {k: snapshot[k] for k in PRESET_FIELDS if k in snapshot}
        for k in PRESET_FIELDS:
            if k not in filtered and k in d:
                filtered[k] = d[k]
        snapshot = filtered
    presets.append({
        "id": secrets.token_urlsafe(8),
        "name": name,
        "slug": slug,
        "created_at": int(time.time()),
        "snapshot": snapshot
    })
    d["presets"] = presets
    save_data(d)
    return jsonify({"ok": True, "presets": presets})


@app.route("/api/presets/<pid>/apply", methods=["POST"])
def apply_preset(pid):
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    d = load_data()
    _, p = _find_preset(d.get("presets", []), pid)
    if not p:
        return jsonify({"ok": False, "error": "preset not found"}), 404
    snap = p.get("snapshot") or {}
    for k in PRESET_FIELDS:
        if k in snap:
            d[k] = snap[k]
    save_data(d)
    return jsonify({"ok": True})


@app.route("/api/presets/<pid>/slug", methods=["POST"])
def update_preset_slug(pid):
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    raw_slug = (body.get("slug") or "").strip().lower().replace(" ", "-")
    slug = re.sub(r'[^a-z0-9_-]', '', raw_slug)[:30]
    if not slug:
        return jsonify({"ok": False, "error": "valid slug required"}), 400
    d = load_data()
    _, p = _find_preset(d.get("presets", []), pid)
    if not p:
        return jsonify({"ok": False, "error": "preset not found"}), 404
    p["slug"] = slug
    save_data(d)
    return jsonify({"ok": True, "presets": d.get("presets", [])})


@app.route("/api/presets/<pid>/duplicate", methods=["POST"])
def duplicate_preset(pid):
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    d = load_data()
    presets = d.get("presets", [])
    if len(presets) >= 50:
        return jsonify({"ok": False, "error": "too many presets (max 50)"}), 400
    idx, p = _find_preset(presets, pid)
    if not p:
        return jsonify({"ok": False, "error": "preset not found"}), 404
    base_name = p.get("name", "preset")
    existing = {x.get("name") for x in presets}
    new_name = f"{base_name} copy"
    n = 2
    while new_name in existing:
        new_name = f"{base_name} copy {n}"
        n += 1
    new_name = new_name[:40]
    base_slug = p.get("slug") or p.get("name", "preset").lower().replace(" ", "-")
    new_slug = re.sub(r'[^a-z0-9_-]', '', f"{base_slug}-copy")[:30]
    copy = {
        "id": secrets.token_urlsafe(8),
        "name": new_name,
        "slug": new_slug,
        "created_at": int(time.time()),
        "snapshot": json.loads(json.dumps(p.get("snapshot") or {}))
    }
    presets.insert(idx + 1, copy)
    d["presets"] = presets
    save_data(d)
    return jsonify({"ok": True, "presets": presets})


@app.route("/api/presets/<pid>/rename", methods=["POST"])
def rename_preset(pid):
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()[:40]
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    d = load_data()
    _, p = _find_preset(d.get("presets", []), pid)
    if not p:
        return jsonify({"ok": False, "error": "preset not found"}), 404
    p["name"] = name
    save_data(d)
    return jsonify({"ok": True, "presets": d["presets"]})



@app.route("/api/public_presets", methods=["GET"])
def get_public_presets():
    d = load_data()
    presets = [
        {"id": p.get("id"), "name": p.get("name"), "slug": p.get("slug") or p.get("name", "").lower().replace(" ", "-")}
        for p in d.get("presets", [])
    ]
    return jsonify({"ok": True, "presets": presets})

@app.route("/api/presets/<pid>", methods=["PUT", "PATCH"])
def update_preset(pid):
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    body = request.get_json(silent=True) or {}
    d = load_data()
    _, p = _find_preset(d.get("presets", []), pid)
    if not p:
        return jsonify({"ok": False, "error": "preset not found"}), 404
    if "name" in body:
        p["name"] = str(body["name"]).strip()[:40] or p["name"]
    if "slug" in body:
        p["slug"] = str(body["slug"]).strip().lower().replace(" ", "-") or p.get("slug", "")
    if "snapshot" in body and isinstance(body["snapshot"], dict):
        p["snapshot"] = body["snapshot"]
    save_data(d)
    return jsonify({"ok": True, "preset": p, "presets": d.get("presets", [])})


@app.route("/api/presets/<pid>", methods=["DELETE"])
def delete_preset(pid):
    if not is_admin(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    d = load_data()
    presets = d.get("presets", [])
    new_presets = [p for p in presets if p.get("id") != pid]
    if len(new_presets) == len(presets):
        return jsonify({"ok": False, "error": "preset not found"}), 404
    d["presets"] = new_presets
    save_data(d)
    return jsonify({"ok": True, "presets": new_presets})



@app.route("/api/presence")
def api_presence():
    d = load_data()
    uid = str(d.get("discord_id", "1373371003425783951")).strip()
    return discord_lookup(uid)

@app.route("/api/visits/count")
def api_visits_count():
    d = load_data()
    cnt = d.get("view_count", 0)
    return jsonify({"count": cnt, "unique": cnt})

@app.route("/api/visit", methods=["POST"])
def api_visit():
    d = load_data()
    d["view_count"] = d.get("view_count", 0) + 1
    save_data(d)
    return jsonify({"count": d["view_count"]})

@app.route("/api/discord/me")
def api_discord_me():
    d = load_data()
    uid = str(d.get("discord_id", "1373371003425783951")).strip()
    return discord_lookup(uid)


@app.route("/api/discord/<user_id>", methods=["GET"])
def discord_lookup(user_id):
    user_id = str(user_id).strip()
    result = {
        "ok": True,
        "id": user_id,
        "username": "user",
        "global_name": None,
        "display_name": "user",
        "avatar_hash": None,
        "avatar_url": "",
        "banner_url": None,
        "accent_color": None,
        "status": "offline",
        "activities": [],
        "custom_status": None,
        "spotify": None,
    }

    has_live_gateway = False
    with PRESENCE_LOCK:
        cached = PRESENCE_CACHE.get(user_id)
        if cached:
            has_live_gateway = True
            result["status"] = cached.get("status", "offline")
            result["custom_status"] = cached.get("custom_status")
            result["spotify"] = cached.get("spotify")

    if DISCORD_BOT_TOKEN:
        try:
            r = requests.get(
                f"https://discord.com/api/v10/users/{user_id}",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"},
                timeout=4
            )
            if r.status_code == 200:
                du = r.json()
                result["username"] = du.get("username") or result["username"]
                result["global_name"] = du.get("global_name")
                result["display_name"] = du.get("global_name") or du.get("username") or result["display_name"]
                avatar = du.get("avatar")
                result["avatar_hash"] = avatar
                if avatar:
                    ext = "gif" if str(avatar).startswith("a_") else "png"
                    result["avatar_url"] = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.{ext}?size=512"
                banner = du.get("banner")
                if banner:
                    ext = "gif" if str(banner).startswith("a_") else "png"
                    result["banner_url"] = f"https://cdn.discordapp.com/banners/{user_id}/{banner}.{ext}?size=1024"
                if du.get("accent_color"):
                    result["accent_color"] = f"#{du['accent_color']:06x}"
        except Exception as e:
            print(f"[discord-api] Error fetching user {user_id}: {e}")

    if not has_live_gateway:
        try:
            r = requests.get(f"https://api.lanyard.rest/v1/users/{user_id}", timeout=3)
            if r.status_code == 200:
                j = r.json()
                if j.get("success") and j.get("data"):
                    ld = j["data"]
                    du = ld.get("discord_user", {})
                    if not result["avatar_url"]:
                        avatar = du.get("avatar")
                        if avatar:
                            ext = "gif" if str(avatar).startswith("a_") else "png"
                            result["avatar_url"] = f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.{ext}?size=512"
                        result["username"] = du.get("username") or result["username"]
                        result["global_name"] = du.get("global_name")
                        result["display_name"] = du.get("global_name") or du.get("username") or result["display_name"]
                    result["status"] = ld.get("discord_status", result["status"])
                    result["spotify"] = ld.get("spotify")
                    
                    found_custom = False
                    for act in ld.get("activities", []):
                        if act.get("type") == 4 or act.get("name") == "Custom Status":
                            st_text = act.get("state") or ""
                            em = act.get("emoji") or {}
                            em_name = em.get("name") or ""
                            em_id = em.get("id")
                            em_url = None
                            if em_id:
                                em_ext = "gif" if em.get("animated") else "png"
                                em_url = f"https://cdn.discordapp.com/emojis/{em_id}.{em_ext}"
                            if st_text or em_name or em_url:
                                result["custom_status"] = {
                                    "text": st_text,
                                    "emoji_name": em_name if not em_id else "",
                                    "emoji_url": em_url
                                }
                                found_custom = True
                            break
                    if not found_custom:
                        result["custom_status"] = None
        except Exception:
            pass

    if not result["avatar_url"]:
        try:
            idx = (int(user_id) >> 22) % 6
        except Exception:
            idx = 0
        result["avatar_url"] = f"https://cdn.discordapp.com/embed/avatars/{idx}.png"

    try:
        d = load_data()
        if user_id == str(d.get("discord_id", "")).strip():
            changed = False
            new_name = result.get("display_name") or result.get("global_name")
            if new_name and d.get("display_name") != new_name:
                d["display_name"] = new_name
                changed = True
            new_handle = result.get("username")
            if new_handle and d.get("custom_handle") != new_handle:
                d["custom_handle"] = new_handle
                changed = True
            if result.get("avatar_url") and (not d.get("custom_avatar_url") or "cdn.discordapp.com/avatars" in str(d.get("custom_avatar_url"))):
                if d.get("custom_avatar_url") != result["avatar_url"]:
                    d["custom_avatar_url"] = result["avatar_url"]
                    changed = True
            if result.get("banner_url") and (not d.get("background_url") or "cdn.discordapp.com/banners" in str(d.get("background_url"))):
                if d.get("background_url") != result["banner_url"]:
                    d["background_url"] = result["banner_url"]
                    changed = True
            if result.get("accent_color") and d.get("accent_color") != result["accent_color"]:
                d["accent_color"] = result["accent_color"]
                changed = True
            if changed:
                save_data(d)
    except Exception:
        pass

    return jsonify(result)


if __name__ == "__main__":
    print(f"[*] chony.info active on http://{BIND_HOST}:{PORT}")
    print(f"[*] Admin Panel: http://{BIND_HOST}:{PORT}/panel (Password: {PASSWORD})")
    app.run(host=BIND_HOST, port=PORT, debug=False)
