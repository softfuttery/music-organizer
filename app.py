import hmac
import os
import secrets
import time
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from music_organizer.auth import is_password_hash, verify_password, write_secret_atomic
from music_organizer.library_routes import create_library_blueprint
from music_organizer.locking import exclusive_file_lock
from music_organizer.lyrics_translation import (
    LyricsTranslationError,
    LyricsTranslationService,
)
from music_organizer.notifications import resolve_magicpush_token, send_magicpush
from music_organizer.review import ReviewRepository
from music_organizer.review_routes import create_review_blueprint
from music_organizer.runtime import runtime_readiness
from music_organizer.security import LoginRateLimiter
from music_organizer.web_config import build_web_config
from organizer import MusicOrganizer

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/config.yaml")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "/app/data/organizer.sqlite3")
LOG_PATH = os.environ.get("LOG_PATH", "/app/data/organizer.log")
PORT = int(os.environ.get("PORT", "5000"))
SOURCE_REVISION = os.environ.get("SOURCE_REVISION", "unknown")
MAGICPUSH_TOKEN_FILE = Path(
    os.environ.get("MAGICPUSH_TOKEN_FILE", "/run/secrets/magicpush/token")
)


def resolve_frontend_dist() -> Path:
    configured = os.environ.get("FRONTEND_DIST")
    if configured:
        return Path(configured)
    project_root = Path(__file__).resolve().parent
    packaged_dist = project_root / "frontend_dist"
    if packaged_dist.joinpath("index.html").is_file():
        return packaged_dist
    return project_root / "frontend-vue" / "dist"


FRONTEND_DIST = resolve_frontend_dist()
WORKER_HEARTBEAT_MAX_AGE_SECONDS = max(
    int(os.environ.get("WORKER_HEARTBEAT_MAX_AGE_SECONDS", "30")), 5
)
REVIEW_WORKER_HEARTBEAT_MAX_AGE_SECONDS = max(
    int(
        os.environ.get(
            "REVIEW_WORKER_HEARTBEAT_MAX_AGE_SECONDS",
            str(WORKER_HEARTBEAT_MAX_AGE_SECONDS),
        )
    ),
    5,
)

def load_secret_key() -> str:
    configured = os.environ.get("SECRET_KEY", "")
    if configured:
        return configured
    configured_path = str(os.environ.get("SECRET_KEY_PATH") or "").strip()
    path = Path(configured_path or str(Path(DATABASE_PATH).parent / ".secret_key"))
    path.parent.mkdir(parents=True, exist_ok=True)
    if configured_path:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError("configured secret key file is unavailable") from exc
        if not value:
            raise RuntimeError("configured secret key file is empty")
        return value

    lock_path = path.with_name(f".{path.name}.generation.lock")
    with exclusive_file_lock(lock_path, timeout=30):
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except FileNotFoundError:
            pass
        value = secrets.token_hex(32)
        write_secret_atomic(path, value)
        return value


app = Flask(__name__)
app.secret_key = load_secret_key()
app.config.update(
    # Enhanced LRC with translations can exceed the small configuration-form
    # payloads previously accepted by the control plane.
    MAX_CONTENT_LENGTH=1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower()
    in {"1", "true", "yes", "on"},
)
trusted_hosts = [
    value.strip()
    for value in os.environ.get("TRUSTED_HOSTS", "").split(",")
    if value.strip()
]
if trusted_hosts:
    app.config["TRUSTED_HOSTS"] = trusted_hosts
login_rate_limiter = LoginRateLimiter(
    max_failures=int(os.environ.get("LOGIN_MAX_FAILURES", "5")),
    window_seconds=float(os.environ.get("LOGIN_WINDOW_SECONDS", "60")),
)
organizer = MusicOrganizer(CONFIG_PATH, DATABASE_PATH, LOG_PATH, file_logging=False)
review_repository = ReviewRepository(DATABASE_PATH)
review_repository.initialize()
app.register_blueprint(
    create_review_blueprint(organizer, review_repository),
    url_prefix="/api/review",
)
app.register_blueprint(
    create_library_blueprint(organizer),
    url_prefix="/api/library",
)


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return str(token)


def auth_required_response():
    if request.path.startswith("/api/"):
        return jsonify({"error": "authentication required"}), 401
    return redirect(url_for("index"))


def read_auth_secret(path: str) -> str:
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError("configured authentication secret is unavailable") from exc
    if not value:
        raise RuntimeError("configured authentication secret is empty")
    return value


def auth_secret() -> str:
    if "AUTH_PASSWORD_HASH" in os.environ:
        value = os.environ.get("AUTH_PASSWORD_HASH", "")
        if value and not is_password_hash(value):
            raise RuntimeError("configured authentication secret is not a password hash")
        return value
    if "AUTH_PASSWORD" in os.environ:
        return os.environ.get("AUTH_PASSWORD", "")
    hash_path = os.environ.get("AUTH_PASSWORD_HASH_FILE", "").strip()
    if hash_path:
        value = read_auth_secret(hash_path)
        if not is_password_hash(value):
            raise RuntimeError("configured authentication secret is not a password hash")
        return value
    path = os.environ.get("AUTH_PASSWORD_FILE", "").strip()
    if not path:
        return ""
    return read_auth_secret(path)


def auth_session_fingerprint(username: str, password: str) -> str:
    key = str(app.secret_key).encode("utf-8")
    value = f"{username}\0{password}".encode("utf-8")
    return hmac.new(key, value, "sha256").hexdigest()


def session_is_authenticated(expected_username: str, expected_password: str) -> bool:
    expected_fingerprint = auth_session_fingerprint(
        expected_username, expected_password
    )
    return (
        bool(session.get("authenticated"))
        and hmac.compare_digest(str(session.get("username", "")), expected_username)
        and hmac.compare_digest(
            str(session.get("auth_fingerprint", "")), expected_fingerprint
        )
    )


@app.before_request
def protect_control_plane():
    if request.endpoint == "api_health":
        return None
    try:
        expected_password = auth_secret()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    expected_username = os.environ.get("AUTH_USERNAME", "admin")
    public_endpoints = {
        "api_csrf",
        "api_login",
        "api_session",
        "favicon",
        "frontend_app_icon",
        "frontend_asset",
        "frontend_manifest",
        "frontend_service_worker",
        "index",
    }
    if (
        expected_password
        and request.endpoint not in public_endpoints
        and not session_is_authenticated(expected_username, expected_password)
    ):
        return auth_required_response()
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        expected = session.get("csrf_token")
        if not supplied or not expected or not hmac.compare_digest(str(supplied), str(expected)):
            return jsonify({"error": "invalid CSRF token"}), 400
    return None


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' https://cdn.jsdelivr.net; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:",
    )
    return response


def build_config_from_form() -> dict:
    return build_web_config(
        organizer.load_config(), request.form, MAGICPUSH_TOKEN_FILE
    )


def config_form_payload() -> dict:
    config = organizer.load_config()
    cue = config.get("cue_split", {})
    qb = config.get("qbittorrent", {})
    review = config.get("review", {})
    translation = config.get("translation", {})
    magicpush = config.get("notifications", {}).get("magicpush", {})
    saved_magicpush_token, _ = resolve_magicpush_token(magicpush)

    return {
        "values": {
            "paths_mapping": "\n".join(
                f"{source} => {target}"
                for source, target in config.get("paths_mapping", {}).items()
            ),
            "mode": config.get("mode", "hardlink"),
            "keep_dir_struct": bool(config.get("keep_dir_struct", True)),
            "mkdir_if_single": bool(config.get("mkdir_if_single", True)),
            "include_globs": "\n".join(config.get("include", {}).get("globs", [])),
            "include_exts": "\n".join(config.get("include", {}).get("exts", [])),
            "exclude_globs": "\n".join(config.get("exclude", {}).get("globs", [])),
            "exclude_exts": "\n".join(config.get("exclude", {}).get("exts", [])),
            "cue_split_enabled": bool(cue.get("enabled")),
            "cue_skip_existing": bool(cue.get("skip_existing", True)),
            "cue_split_multifile_cues": bool(cue.get("split_multifile_cues")),
            "cue_skip_source_audio": bool(cue.get("skip_source_audio", True)),
            "cue_ffmpeg_path": cue.get("ffmpeg_path") or "ffmpeg",
            "cue_flac_compression_level": cue.get("flac_compression_level", 6),
            "cue_output_subdir": cue.get("output_subdir", ""),
            "cue_filename_template": cue.get("filename_template") or "{track:02d} - {title}",
            "qb_enabled": bool(qb.get("enabled")),
            "qb_base_url": qb.get("base_url", ""),
            "qb_username": qb.get("username", ""),
            "qb_password": "",
            "qb_api_key": "",
            "qb_timeout": qb.get("timeout", 10),
            "qb_min_completion_age_seconds": qb.get("min_completion_age_seconds", 60),
            "qb_scan_mode": qb.get("scan_mode", "torrent_paths"),
            "qb_poll_mode": qb.get("poll_mode", "sync"),
            "qb_category": qb.get("category", ""),
            "qb_tag": qb.get("tag", ""),
            "review_enabled": bool(review.get("enabled")),
            "review_auto_discover": bool(review.get("auto_discover", True)),
            "review_discovery_interval_seconds": review.get("discovery_interval_seconds", 60),
            "review_discovery_stable_seconds": review.get("discovery_stable_seconds", 60),
            "review_identify_workers": review.get("identify_workers", 3),
            "review_proxy_url": review.get("proxy_url", ""),
            "review_proxy_username": review.get("proxy_username", ""),
            "review_proxy_password": "",
            "review_source_roots": "\n".join(review.get("source_roots", [])),
            "review_source_profiles": review.get("source_profiles", []),
            "review_directory": review.get("directory", ""),
            "review_recycle_directory": review.get("recycle_directory", ""),
            "review_library": review.get("library", ""),
            "review_config_path": review.get("config_path", ""),
            "review_import_mode": review.get("import_mode", "hardlink"),
            "review_write_tags": bool(review.get("write_tags")),
            "review_move_extra_files": bool(review.get("move_extra_files")),
            "review_cleanup_source_after_import": bool(review.get("cleanup_source_after_import")),
            "review_extra_file_patterns": " ".join(review.get("extra_file_patterns", [])),
            "review_path_format": review.get("path_format", ""),
            "translation_enabled": bool(translation.get("enabled")),
            "translation_base_url": translation.get("base_url", "https://api.deepseek.com"),
            "translation_model": translation.get("model", "deepseek-v4-flash"),
            "translation_api_key": "",
            "translation_style": translation.get("style", "natural"),
            "translation_timeout": translation.get("timeout", 120),
            "magicpush_enabled": bool(magicpush.get("enabled")),
            "magicpush_base_url": magicpush.get("base_url", ""),
            "magicpush_timeout": magicpush.get("timeout", 10),
            "magicpush_title": magicpush.get("title", "Music Organizer"),
            "magicpush_token": "",
            "magicpush_notify_no_changes": bool(magicpush.get("notify_no_changes")),
            "schedule_cron": config.get("schedule", {}).get("cron", "*/30 * * * *"),
            "schedule_enabled": bool(config.get("schedule", {}).get("enabled", True)),
            "progress_interval": config.get("logging", {}).get("progress_interval", 500),
            "verbose_file_actions": bool(config.get("logging", {}).get("verbose_file_actions")),
        },
        "saved": {
            "qb_password": bool(qb.get("password")),
            "qb_api_key": bool(qb.get("api_key")),
            "review_proxy_password": bool(review.get("proxy_password")),
            "translation_api_key": bool(translation.get("api_key")),
            "magicpush_token": bool(saved_magicpush_token),
        },
    }


def worker_is_fresh(state: dict[str, str] | None = None) -> bool:
    value = (
        state.get("worker_heartbeat", "")
        if state is not None
        else organizer.app_state_value("worker_heartbeat", "")
    )
    if not value:
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(value)).total_seconds()
    except ValueError:
        return False
    return 0 <= age <= WORKER_HEARTBEAT_MAX_AGE_SECONDS


def review_worker_status(state: dict[str, str] | None = None) -> str:
    def state_value(key: str, default: str = "") -> str:
        if state is not None:
            return str(state.get(key, default))
        return organizer.app_state_value(key, default)

    heartbeat_value = state_value("review_worker_heartbeat", "")
    if not heartbeat_value:
        return "stale"
    try:
        now = datetime.now()
        heartbeat_age = (
            now - datetime.fromisoformat(heartbeat_value)
        ).total_seconds()
    except ValueError:
        return "stale"
    if not 0 <= heartbeat_age <= REVIEW_WORKER_HEARTBEAT_MAX_AGE_SECONDS:
        return "stale"

    import_started = state_value("review_import_active_at", "")
    if not import_started:
        return "ok"
    try:
        import_age = (now - datetime.fromisoformat(import_started)).total_seconds()
        import_timeout = float(
            state_value("review_import_timeout_seconds", "")
            or os.environ.get("REVIEW_IMPORT_TIMEOUT_SECONDS", "")
            or 3600
        )
    except (TypeError, ValueError):
        return "stuck"
    if import_age < 0 or import_age > max(import_timeout, 60) + 30:
        return "stuck"
    return "ok"


def dashboard_stats() -> dict:
    data = organizer.stats()
    review_counts = review_repository.scope_counts()
    data["review_active"] = review_counts["active"]
    data["review_archived"] = review_counts["archived"]
    return data


def aggregate_runtime_health(
    *,
    state: dict[str, str] | None = None,
    review_enabled: bool | None = None,
) -> tuple[dict, int]:
    """Return worker health after the caller has already checked SQLite."""
    worker_status = "ok" if worker_is_fresh(state) else "stale"
    if review_enabled is None:
        try:
            review_enabled = bool(
                organizer.load_config().get("review", {}).get("enabled", False)
            )
        except Exception as exc:
            return (
                {
                    "status": "error",
                    "configuration": str(exc),
                    "web": "ok",
                    "worker": worker_status,
                    "source_revision": SOURCE_REVISION,
                },
                503,
            )
    current_review_worker_status = (
        review_worker_status(state) if review_enabled else "disabled"
    )
    all_workers_ok = (
        worker_status == "ok"
        and current_review_worker_status in {"ok", "disabled"}
    )
    return (
        {
            "status": "ok" if all_workers_ok else "degraded",
            "web": "ok",
            "worker": worker_status,
            "review_worker": current_review_worker_status,
            "source_revision": SOURCE_REVISION,
        },
        200 if all_workers_ok else 503,
    )


def dashboard_payload() -> dict:
    """Build one consistent UI snapshot without duplicate polling queries."""
    config = organizer.load_config()
    snapshot = organizer.repository.dashboard_runtime_snapshot()
    state = snapshot["app_state"]
    data = organizer.stats(config=config, snapshot=snapshot)
    data["review_active"] = snapshot["review_counts"]["active"]
    data["review_archived"] = snapshot["review_counts"]["archived"]
    data["next_run_time"] = state.get("next_run_time") or None
    data["job_status"] = snapshot["job_status"]
    data["worker_running"] = worker_is_fresh(state)
    data["health"] = aggregate_runtime_health(
        state=state,
        review_enabled=bool(config.get("review", {}).get("enabled", False)),
    )[0]
    data["qb_connection"] = {
        "status": state.get("qb_last_status", "unknown"),
        "last_attempt_at": state.get("qb_last_attempt_at", ""),
        "last_success_at": state.get("qb_last_success_at", ""),
        "last_error": state.get("qb_last_error", ""),
    }
    return data


@app.context_processor
def inject_template_helpers():
    return {"now": datetime.now, "csrf_token": csrf_token}


@app.route("/")
def index():
    if FRONTEND_DIST.joinpath("index.html").is_file():
        return send_from_directory(FRONTEND_DIST, "index.html")
    expected_password = auth_secret()
    expected_username = os.environ.get("AUTH_USERNAME", "admin")
    if expected_password and not session_is_authenticated(
        expected_username, expected_password
    ):
        return "Vue frontend build is unavailable", 503
    return render_template(
        "index.html",
        stats=dashboard_stats(),
        next_run_time=organizer.app_state_value("next_run_time", "") or None,
        job_status=organizer.repository.job_snapshot(),
    )


@app.route("/assets/<path:filename>")
def frontend_asset(filename: str):
    return send_from_directory(FRONTEND_DIST / "assets", filename)


@app.route("/manifest.webmanifest")
def frontend_manifest():
    return send_from_directory(
        FRONTEND_DIST,
        "manifest.webmanifest",
        mimetype="application/manifest+json",
    )


@app.route("/sw.js")
def frontend_service_worker():
    response = send_from_directory(
        FRONTEND_DIST,
        "sw.js",
        mimetype="application/javascript",
    )
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/app-icon.svg")
def frontend_app_icon():
    return send_from_directory(
        FRONTEND_DIST,
        "app-icon.svg",
        mimetype="image/svg+xml",
    )


@app.route("/review")
def review_page():
    if FRONTEND_DIST.joinpath("index.html").is_file():
        return send_from_directory(FRONTEND_DIST, "index.html")
    return "Vue frontend build is unavailable", 503


@app.route("/library")
def library_page():
    if FRONTEND_DIST.joinpath("index.html").is_file():
        return send_from_directory(FRONTEND_DIST, "index.html")
    return "Vue frontend build is unavailable", 503


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.svg",
        mimetype="image/svg+xml",
    )


@app.route("/history")
def history():
    if FRONTEND_DIST.joinpath("index.html").is_file():
        return send_from_directory(FRONTEND_DIST, "index.html")
    page = max(request.args.get("page", 1, type=int), 1)
    query = request.args.get("q", "", type=str).strip()
    return render_template(
        "history.html", history=organizer.history(page=page, per_page=50, query=query)
    )


@app.route("/config", methods=["GET", "POST"])
def config_page():
    if request.method == "GET" and FRONTEND_DIST.joinpath("index.html").is_file():
        return send_from_directory(FRONTEND_DIST, "index.html")
    if request.method == "POST":
        try:
            config_to_save = build_config_from_form()
            organizer.save_config(config_to_save)
            submitted_token = request.form.get("magicpush_token", "").strip()
            if submitted_token:
                write_secret_atomic(MAGICPUSH_TOKEN_FILE, submitted_token)
            flash("配置已保存，Worker 将自动重新加载", "success")
            return redirect(url_for("config_page"))
        except (TypeError, ValueError) as exc:
            flash(str(exc), "danger")
        except Exception:
            app.logger.exception("Legacy configuration form save failed")
            flash("配置保存失败，请查看服务日志", "danger")
    config = organizer.load_config()
    qbittorrent = config.get("qbittorrent", {})
    review = config.get("review", {})
    magicpush = config.get("notifications", {}).get("magicpush", {})
    translation = config.get("translation", {})
    saved_magicpush_token, _ = resolve_magicpush_token(magicpush)
    return render_template(
        "config.html",
        config=config,
        paths_mapping="\n".join(
            f"{source} => {target}"
            for source, target in config.get("paths_mapping", {}).items()
        ),
        include_globs="\n".join(config.get("include", {}).get("globs", [])),
        include_exts="\n".join(config.get("include", {}).get("exts", [])),
        exclude_globs="\n".join(config.get("exclude", {}).get("globs", [])),
        exclude_exts="\n".join(config.get("exclude", {}).get("exts", [])),
        cue_split=config.get("cue_split", {}),
        qbittorrent=qbittorrent,
        qb_password_saved=bool(qbittorrent.get("password")),
        qb_api_key_saved=bool(qbittorrent.get("api_key")),
        review=review,
        review_proxy_password_saved=bool(review.get("proxy_password")),
        translation=translation,
        translation_api_key_saved=bool(translation.get("api_key")),
        magicpush=magicpush,
        magicpush_token_saved=bool(saved_magicpush_token),
    )


@app.get("/api/history")
def api_history():
    page = max(request.args.get("page", 1, type=int), 1)
    query = request.args.get("q", "", type=str).strip()
    return jsonify(organizer.history(page=page, per_page=50, query=query))


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(config_form_payload())
    try:
        config_to_save = build_config_from_form()
        organizer.save_config(config_to_save)
        submitted_token = request.form.get("magicpush_token", "").strip()
        if submitted_token:
            write_secret_atomic(MAGICPUSH_TOKEN_FILE, submitted_token)
        payload = config_form_payload()
        payload["message"] = "配置已保存，Worker 将自动重新加载"
        return jsonify(payload)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


def enqueue_job(job_type: str):
    created, status = organizer.repository.enqueue_job(job_type)
    return jsonify(status), 202 if created else 409



@app.route("/api/trigger", methods=["POST"])
def api_trigger():
    return enqueue_job("manual_scan")


@app.route("/api/qb/trigger", methods=["POST"])
def api_qb_trigger():
    return enqueue_job("qb_poll")


@app.route("/api/qb/retry/<torrent_hash>", methods=["POST"])
def api_qb_retry(torrent_hash: str):
    if not organizer.repository.reset_qb_torrent_retry(torrent_hash):
        return jsonify({"error": "没有找到需要人工重试的 qBittorrent 任务"}), 404
    # A sync poll may already have advanced past this torrent while it was in
    # needs_attention. Force one full qBittorrent snapshot so it is visible.
    organizer.set_app_state_value("qb_sync_rid", "0")
    created, status = organizer.repository.enqueue_job("qb_poll")
    status["retry_reset"] = True
    return jsonify(status), 202 if created else 409


@app.post("/api/lyrics/translate")
def translate_lyrics():
    payload = request.get_json(silent=True) or {}
    try:
        config = organizer.load_config()
        result = LyricsTranslationService(
            config.get("translation", {})
        ).translate(
            payload.get("content", ""),
            title=str(payload.get("title") or ""),
            artist=str(payload.get("artist") or ""),
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except LyricsTranslationError as exc:
        return jsonify({"error": str(exc)}), 502


@app.route("/api/stop", methods=["POST"])
def api_stop():
    stopped, status = organizer.repository.request_cancel_active_job()
    return jsonify(status), 202 if stopped else 409


@app.route("/api/stats")
def api_stats():
    return jsonify(dashboard_payload())


@app.get("/api/dashboard")
def api_dashboard():
    return jsonify(dashboard_payload())


@app.route("/api/job")
def api_job():
    return jsonify(organizer.repository.job_snapshot())


@app.route("/api/session")
def api_session():
    expected_password = auth_secret()
    expected_username = os.environ.get("AUTH_USERNAME", "admin")
    authenticated = not expected_password or session_is_authenticated(
        expected_username, expected_password
    )
    return jsonify(
        {
            "authenticated": authenticated,
            "authentication_enabled": bool(expected_password),
            "username": expected_username,
        }
    )


@app.route("/api/login", methods=["POST"])
def api_login():
    rate_limit_key = request.remote_addr or "unknown"
    retry_after = login_rate_limiter.retry_after(rate_limit_key)
    if retry_after:
        response = jsonify({"error": "登录失败次数过多，请稍后重试"})
        response.headers["Retry-After"] = str(retry_after)
        return response, 429
    expected_password = auth_secret()
    expected_username = os.environ.get("AUTH_USERNAME", "admin")
    payload = request.get_json(silent=True) or request.form
    supplied_username = str(payload.get("username", ""))
    supplied_password = str(payload.get("password", ""))
    username_matches = hmac.compare_digest(supplied_username, expected_username)
    password_matches = verify_password(expected_password, supplied_password)
    if expected_password and not (username_matches and password_matches):
        login_rate_limiter.record_failure(rate_limit_key)
        time.sleep(0.25)
        return jsonify({"error": "用户名或密码错误"}), 401
    login_rate_limiter.reset(rate_limit_key)
    session.clear()
    session["authenticated"] = True
    session["username"] = expected_username
    session["auth_fingerprint"] = auth_session_fingerprint(
        expected_username, expected_password
    )
    return jsonify(
        {
            "authenticated": True,
            "username": expected_username,
            "csrf_token": csrf_token(),
        }
    )


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"authenticated": False, "csrf_token": csrf_token()})


@app.route("/api/csrf")
def api_csrf():
    return jsonify({"token": csrf_token()})


@app.route("/api/logs")
def api_logs():
    limit = min(max(request.args.get("limit", 200, type=int), 1), 1000)
    return jsonify({"logs": organizer.recent_logs(limit=limit)})



@app.route("/api/notifications/magicpush/test", methods=["POST"])
def api_magicpush_test():
    config = organizer.load_config()
    magicpush = dict(config.get("notifications", {}).get("magicpush", {}))
    magicpush["enabled"] = True
    title_prefix = str(magicpush.get("title") or "Music Organizer")
    result = send_magicpush(
        magicpush,
        "这是一条来自 Music Organizer 配置页的 MagicPush 测试消息。",
        title=f"[测试] {title_prefix}",
    )
    if result and result.get("sent"):
        return jsonify(
            {
                "sent": True,
                "status_code": result.get("status_code"),
                "message": "MagicPush 测试消息已发送",
            }
        )
    return (
        jsonify(
            {
                "sent": False,
                "status_code": (result or {}).get("status_code"),
                "error": (result or {}).get("error") or "MagicPush 未发送",
            }
        ),
        502,
    )


@app.route("/api/health")
def api_health():
    try:
        auth_secret()
    except RuntimeError as exc:
        return jsonify(
            {
                "status": "error",
                "authentication": str(exc),
                "source_revision": SOURCE_REVISION,
            }
        ), 503
    try:
        organizer.repository.dashboard_snapshot()
    except Exception as exc:
        return jsonify(
            {
                "status": "error",
                "database": str(exc),
                "source_revision": SOURCE_REVISION,
            }
        ), 503
    readiness = runtime_readiness(CONFIG_PATH, DATABASE_PATH)
    if readiness["status"] != "ok":
        return jsonify(
            {
                "status": "error",
                "runtime": readiness["failed"],
                "source_revision": SOURCE_REVISION,
            }
        ), 503
    component = str(request.args.get("component") or "").strip().lower()
    component = component.replace("_", "-")
    if component not in {"", "web", "worker", "review-worker"}:
        return jsonify(
            {
                "status": "error",
                "component": component,
                "error": "unknown health component",
                "source_revision": SOURCE_REVISION,
            }
        ), 400
    if component == "web":
        return jsonify(
            {
                "status": "ok",
                "component": "web",
                "source_revision": SOURCE_REVISION,
            }
        )
    worker_status = "ok" if worker_is_fresh() else "stale"
    if component == "worker":
        return (
            jsonify(
                {
                    "status": "ok" if worker_status == "ok" else "degraded",
                    "component": "worker",
                    "worker": worker_status,
                    "source_revision": SOURCE_REVISION,
                }
            ),
            200 if worker_status == "ok" else 503,
        )
    if component == "review-worker":
        current_review_worker_status = review_worker_status()
        return (
            jsonify(
                {
                    "status": (
                        "ok"
                        if current_review_worker_status == "ok"
                        else "degraded"
                    ),
                    "component": "review-worker",
                    "review_worker": current_review_worker_status,
                    "source_revision": SOURCE_REVISION,
                }
            ),
            200 if current_review_worker_status == "ok" else 503,
        )
    health, status_code = aggregate_runtime_health()
    return jsonify(health), status_code


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
