"""HTTP routes for the manual music review workflow."""

import sqlite3
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from flask import Blueprint, jsonify, request, send_file

from .lyrics import LyricsProviderError, LyricsSearchService
from .manual_review import build_manual_candidate
from .review import (
    AUDIO_EXTENSIONS,
    ActiveReviewOverlapError,
    ReviewRepository,
    audio_files,
    ensure_within_roots,
)


def create_review_blueprint(
    organizer: Any,
    repository: ReviewRepository,
) -> Blueprint:
    blueprint = Blueprint("review_api", __name__)

    def review_settings() -> tuple[dict, list[Path]]:
        review = organizer.load_config().get("review", {})
        roots = [Path(value) for value in review.get("source_roots", []) if value]
        return review, roots

    def item_audio_path(item_id: int, raw_path: str) -> tuple[dict, Path]:
        item = repository.item(item_id)
        relative = PurePosixPath(str(raw_path or "").strip())
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("音频路径无效")
        root = Path(item["source_path"])
        if root.is_symlink():
            raise ValueError("预审源目录不能是符号链接")
        resolved_root = root.resolve(strict=True)
        current = resolved_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("音频路径不能包含符号链接")
        resolved = current.resolve(strict=True)
        if (
            not resolved.is_file()
            or not resolved.is_relative_to(resolved_root)
            or resolved.suffix.lower() not in AUDIO_EXTENSIONS
        ):
            raise ValueError("音频文件不在当前预审项目中")
        return item, resolved

    def audio_duration(path: Path) -> float:
        try:
            from mutagen import File, MutagenError

            media = File(str(path), easy=False)
            return round(float(getattr(getattr(media, "info", None), "length", 0) or 0), 3)
        except (MutagenError, OSError, TypeError, ValueError):
            return 0.0

    def lyric_service(review: dict) -> LyricsSearchService:
        return LyricsSearchService(review, timeout=10)

    @blueprint.get("/roots")
    def roots():
        review, source_roots = review_settings()
        if not review.get("enabled", False):
            return jsonify({"enabled": False, "roots": [], "directories": []})
        requested = request.args.get("path", "").strip()
        current = (
            ensure_within_roots(requested, source_roots)
            if requested
            else None
        )
        directories = []
        if current is not None:
            for child in sorted(
                current.iterdir(),
                key=lambda item: item.name.casefold(),
            ):
                if (
                    child.is_dir()
                    and not child.is_symlink()
                    and not child.name.startswith(".music-organizer-")
                    and child.name != "#recycle"
                ):
                    directories.append({"name": child.name, "path": str(child)})
        return jsonify(
            {
                "enabled": True,
                "roots": [
                    {"name": root.name or str(root), "path": str(root)}
                    for root in source_roots
                ],
                "current": str(current) if current else None,
                "directories": directories,
            }
        )

    @blueprint.get("/files")
    def files():
        review, source_roots = review_settings()
        if not review.get("enabled", False):
            return jsonify({"error": "音乐预审功能尚未启用"}), 409
        requested = request.args.get("path", "").strip()
        if not requested:
            return jsonify({"error": "缺少目录路径"}), 400
        try:
            current = ensure_within_roots(requested, source_roots)
            paths = audio_files(current)
            limit = 500
            return jsonify(
                {
                    "path": str(current),
                    "files": [
                        {
                            "name": path.name,
                            "relative_path": path.relative_to(current).as_posix(),
                            "extension": path.suffix.lower(),
                            "size": path.stat().st_size,
                        }
                        for path in paths[:limit]
                    ],
                    "total": len(paths),
                    "truncated": len(paths) > limit,
                }
            )
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @blueprint.get("/items/<int:item_id>/audio")
    def item_audio(item_id: int):
        try:
            _item, path = item_audio_path(item_id, request.args.get("path", ""))
            response = send_file(path, conditional=True, max_age=300)
            response.headers["Cache-Control"] = "private, max-age=300"
            return response
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

    @blueprint.post("/items/<int:item_id>/lyrics/search")
    def search_lyrics(item_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            review, _source_roots = review_settings()
            _item, path = item_audio_path(item_id, payload.get("local_path", ""))
            duration = payload.get("duration") or audio_duration(path)
            result = lyric_service(review).search(
                str(payload.get("title") or ""),
                str(payload.get("artist") or ""),
                duration,
                payload.get("sources"),
                album=str(payload.get("album") or ""),
                artist_aliases=payload.get("artist_aliases"),
            )
            result["duration"] = duration
            return jsonify(result)
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @blueprint.post("/items/<int:item_id>/lyrics/fetch")
    def fetch_lyrics(item_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            review, _source_roots = review_settings()
            item_audio_path(item_id, payload.get("local_path", ""))
            candidate = payload.get("candidate")
            if not isinstance(candidate, dict):
                raise ValueError("歌词候选必须是对象")
            return jsonify(lyric_service(review).fetch(candidate))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except LyricsProviderError as exc:
            return jsonify({"error": str(exc)}), 502

    @blueprint.post("/items/<int:item_id>/lyrics/save")
    def save_lyrics(item_id: int):
        payload = request.get_json(silent=True) or {}
        local_path = str(payload.get("local_path") or "")
        decision = payload.get("decision")
        if not isinstance(decision, dict):
            return jsonify({"error": "歌词决定必须是对象"}), 400
        try:
            item_audio_path(item_id, local_path)
            return jsonify(
                repository.save_lyric_decision(item_id, local_path, decision)
            )
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @blueprint.route("/batches", methods=["GET", "POST"])
    def batches():
        if request.method == "GET":
            limit = min(max(request.args.get("limit", 30, type=int), 1), 100)
            scope = request.args.get(
                "scope", "active", type=str
            ).strip().lower()
            query = request.args.get("q", "", type=str).strip()
            try:
                values = repository.batches(limit, scope=scope, query=query)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            return jsonify(
                {
                    "batches": values,
                    "scope": scope,
                    "counts": repository.scope_counts(),
                }
            )

        review, source_roots = review_settings()
        if not review.get("enabled", False):
            return jsonify({"error": "音乐预审功能尚未启用"}), 409
        payload = request.get_json(silent=True) or {}
        raw_paths = payload.get("paths") or []
        if not isinstance(raw_paths, list):
            return jsonify({"error": "paths 必须是目录列表"}), 400
        if len(raw_paths) > 100:
            return jsonify({"error": "单个批次最多选择 100 个目录"}), 400
        try:
            paths = []
            seen = set()
            for value in raw_paths:
                resolved = ensure_within_roots(str(value), source_roots)
                if resolved not in seen:
                    paths.append(resolved)
                    seen.add(resolved)
            batch = repository.create_batch(
                paths,
                str(payload.get("label") or ""),
            )
        except ActiveReviewOverlapError as exc:
            return jsonify({"error": str(exc)}), 409
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(batch), 202

    @blueprint.get("/batches/<int:batch_id>")
    def batch(batch_id: int):
        scope = request.args.get(
            "scope", "active", type=str
        ).strip().lower()
        query = request.args.get("q", "", type=str).strip()
        try:
            return jsonify(repository.batch(batch_id, scope=scope, query=query))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @blueprint.post("/items/<int:item_id>/approve")
    def approve(item_id: int):
        payload = request.get_json(silent=True) or {}
        candidate_key = str(payload.get("candidate_key") or "").strip()
        if not candidate_key:
            return jsonify({"error": "请选择一个识别候选"}), 400
        track_mapping = payload.get("track_mapping")
        quarantine_paths = payload.get("quarantine_paths")
        if track_mapping is not None and not isinstance(track_mapping, list):
            return jsonify({"error": "track_mapping 必须是列表"}), 400
        if quarantine_paths is not None and not isinstance(
            quarantine_paths, list
        ):
            return jsonify({"error": "quarantine_paths 必须是列表"}), 400
        try:
            item = repository.approve(
                item_id,
                candidate_key,
                track_mapping=track_mapping,
                quarantine_paths=quarantine_paths,
            )
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except (sqlite3.IntegrityError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(item), 202

    @blueprint.get("/items/<int:item_id>/manual-preview")
    def manual_preview(item_id: int):
        try:
            item = repository.item(item_id)
            return jsonify(
                build_manual_candidate(
                    item["source_path"],
                    current_artist=item.get("current_artist", ""),
                    current_album=item.get("current_album", ""),
                )
            )
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @blueprint.post("/items/<int:item_id>/approve-manual")
    def approve_manual(item_id: int):
        payload = request.get_json(silent=True) or {}
        quarantine_paths = payload.get("quarantine_paths")
        if quarantine_paths is not None and not isinstance(quarantine_paths, list):
            return jsonify({"error": "quarantine_paths 必须是列表"}), 400
        try:
            item = repository.item(item_id)
            candidate = build_manual_candidate(
                item["source_path"],
                payload,
                current_artist=item.get("current_artist", ""),
                current_album=item.get("current_album", ""),
            )
            return jsonify(
                repository.approve_manual(
                    item_id,
                    candidate,
                    quarantine_paths=quarantine_paths,
                )
            ), 202
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except (sqlite3.IntegrityError, OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    @blueprint.post("/items/<int:item_id>/identify")
    def reidentify(item_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            item = repository.reidentify(
                item_id,
                search_artist=str(payload.get("artist") or ""),
                search_album=str(payload.get("album") or ""),
                release_id=str(payload.get("release_id") or ""),
            )
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except (sqlite3.IntegrityError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(item), 202

    @blueprint.post("/items/<int:item_id>/skip")
    def skip(item_id: int):
        try:
            item = repository.skip(item_id)
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(item)

    @blueprint.delete("/items/<int:item_id>/archive")
    def delete_archive(item_id: int):
        try:
            return jsonify(repository.delete_archived_item(item_id))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 409

    @blueprint.post("/items/<int:item_id>/recycle-source")
    def recycle_source(item_id: int):
        payload = request.get_json(silent=True) or {}
        try:
            review, source_roots = review_settings()
            if not review.get("enabled", False):
                return jsonify({"error": "音乐预审功能尚未启用"}), 409
            item = repository.item(item_id)
            if item["status"] not in ("ready", "needs_review", "failed"):
                raise ValueError("当前状态不能移动源目录")
            if str(payload.get("confirm_path") or "") != item["source_path"]:
                raise ValueError("回收确认路径与预审目录不一致")

            raw_source = Path(item["source_path"])
            if raw_source.is_symlink():
                raise ValueError("不能移动符号链接形式的预审目录")
            source = ensure_within_roots(raw_source, source_roots)
            resolved_roots = [
                root.expanduser().resolve(strict=True) for root in source_roots
            ]
            if source in resolved_roots:
                raise ValueError("不能移动预审根目录")

            recycle_value = str(review.get("recycle_directory") or "").strip()
            if not recycle_value:
                raise ValueError("请先在配置页设置回收目录")
            recycle = Path(recycle_value).expanduser()
            if not recycle.is_absolute():
                raise ValueError("回收目录必须使用容器内绝对路径")
            if recycle.is_symlink():
                raise ValueError("回收目录不能是符号链接")
            recycle_parent = recycle.parent.resolve(strict=True)
            if not recycle_parent.is_dir():
                raise ValueError("回收目录的父目录不可用")
            recycle.mkdir(exist_ok=True)
            recycle = recycle.resolve(strict=True)
            if recycle == source or recycle.is_relative_to(source):
                raise ValueError("回收目录不能位于待移动的专辑目录内")

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            destination = recycle / f"{stamp}-review-{item_id}-{source.name}"
            suffix = 1
            while destination.exists() or destination.is_symlink():
                destination = recycle / f"{stamp}-review-{item_id}-{source.name}-{suffix}"
                suffix += 1

            source.replace(destination)
            return jsonify(repository.record_source_recycled(item_id, destination))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 404
        except (OSError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 409

    return blueprint
