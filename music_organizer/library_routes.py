"""HTTP routes for directly managing the configured destination music library."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request, send_file

from .library_manager import (
    audio_duration,
    library_file,
    library_root,
    restore_trash,
    save_lyrics,
    scan_folders,
    scan_tracks,
    track_detail,
    trash_entries,
    trash_folder,
    trash_track,
    update_tags,
)
from .lyrics import LyricsProviderError, LyricsSearchService


def create_library_blueprint(organizer: Any) -> Blueprint:
    blueprint = Blueprint("library_api", __name__)

    def settings() -> tuple[dict[str, Any], Any]:
        config = organizer.load_config()
        review = dict(config.get("review", {}))
        directory = str(review.get("directory") or "").strip()
        if not directory:
            raise ValueError("请先配置音乐预审目标目录")
        return review, library_root(directory)

    @blueprint.get("/tracks")
    def tracks():
        try:
            _review, root = settings()
            return jsonify(
                scan_tracks(
                    root,
                    query=request.args.get("q", "", type=str),
                    offset=request.args.get("offset", 0, type=int),
                    limit=request.args.get("limit", 100, type=int),
                )
            )
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @blueprint.get("/folders")
    def folders():
        try:
            _review, root = settings()
            return jsonify(
                scan_folders(
                    root,
                    query=request.args.get("q", "", type=str),
                    offset=request.args.get("offset", 0, type=int),
                    limit=request.args.get("limit", 20, type=int),
                    order=request.args.get("order", "desc", type=str),
                )
            )
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @blueprint.get("/track")
    def track():
        try:
            _review, root = settings()
            path = library_file(root, request.args.get("path", ""))
            response = jsonify(track_detail(path, root))
            response.headers["Cache-Control"] = "no-store"
            return response
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @blueprint.post("/track/update")
    def update_track():
        payload = request.get_json(silent=True) or {}
        tags = payload.get("tags")
        if not isinstance(tags, dict):
            return jsonify({"error": "tags 必须是对象"}), 400
        try:
            _review, root = settings()
            path = library_file(root, payload.get("path", ""))
            return jsonify({"tags": update_tags(path, tags)})
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @blueprint.get("/audio")
    def audio():
        try:
            _review, root = settings()
            path = library_file(root, request.args.get("path", ""))
            response = send_file(path, conditional=True, max_age=300)
            response.headers["Cache-Control"] = "private, max-age=300"
            return response
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @blueprint.post("/lyrics/search")
    def search_lyrics():
        payload = request.get_json(silent=True) or {}
        try:
            review, root = settings()
            path = library_file(root, payload.get("path", ""))
            duration = payload.get("duration") or audio_duration(path)
            result = LyricsSearchService(review, timeout=10).search(
                str(payload.get("title") or ""),
                str(payload.get("artist") or ""),
                duration,
                payload.get("sources"),
                album=str(payload.get("album") or ""),
                artist_aliases=payload.get("artist_aliases"),
            )
            result["duration"] = duration
            return jsonify(result)
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @blueprint.post("/lyrics/fetch")
    def fetch_lyrics():
        payload = request.get_json(silent=True) or {}
        candidate = payload.get("candidate")
        if not isinstance(candidate, dict):
            return jsonify({"error": "歌词候选必须是对象"}), 400
        try:
            review, root = settings()
            library_file(root, payload.get("path", ""))
            return jsonify(LyricsSearchService(review, timeout=10).fetch(candidate))
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        except LyricsProviderError as exc:
            return jsonify({"error": str(exc)}), 502

    @blueprint.post("/lyrics/save")
    def persist_lyrics():
        payload = request.get_json(silent=True) or {}
        try:
            _review, root = settings()
            path = library_file(root, payload.get("path", ""))
            return jsonify(
                save_lyrics(path, payload.get("content", ""), payload.get("mode", ""))
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @blueprint.get("/trash")
    def trash():
        try:
            _review, root = settings()
            return jsonify({"entries": trash_entries(root)})
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @blueprint.post("/trash")
    def move_to_trash():
        payload = request.get_json(silent=True) or {}
        try:
            _review, root = settings()
            return jsonify(trash_track(root, payload.get("path", "")))
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @blueprint.post("/trash/folder")
    def move_folder_to_trash():
        payload = request.get_json(silent=True) or {}
        try:
            _review, root = settings()
            return jsonify(trash_folder(root, payload.get("path", "")))
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @blueprint.post("/trash/restore")
    def restore():
        payload = request.get_json(silent=True) or {}
        try:
            _review, root = settings()
            return jsonify(restore_trash(root, payload.get("token", "")))
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    return blueprint
