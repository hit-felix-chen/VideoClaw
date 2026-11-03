# -*- coding: utf-8 -*-
"""Global character/setting library.

Records use the same shape as a session asset (`id`, `name`, `description`,
`selected`, `versions`) plus `type` (`characters` or `settings`). Bytes and
JSON live under `code/data/assets/{asset_id}/`.

Do not store this under `code/result/image/`. `cleanup_orphan_results` deletes
every child of `code/result/image/` and `code/result/video/` that is not a
session id, which would remove a library on the next orphan cleanup.

Session version lookup is a prefix glob (`{asset_id}*.png` in
`CharacterDesignerAgent._list_versions` and `{segment_id}*.mp4` in
`VideoDirectorAgent._list_versions`). Library ids are a fixed-length
`lib` + 32 hex chars, and each asset's files live in their own directory
named `v1.png`, `v2.png`, ... so one library id cannot prefix-match another
and library bytes never sit beside session version files.
"""

import json
import os
import re
import shutil
import threading
import uuid
from typing import Any, Dict, List, Optional

from config import settings

_LOCK = threading.RLock()
_ID_RE = re.compile(r"^lib[0-9a-f]{32}$")
_VERSION_RE = re.compile(r"^v(\d+)$")
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
ASSET_TYPES = ("characters", "settings")


class AssetLibrary:
    """JSON + image files for the cross-session asset library."""

    @staticmethod
    def root() -> str:
        return os.path.join(settings.CODE_DIR, "data", "assets")

    @staticmethod
    def file_exists(path: str) -> bool:
        if not path or not isinstance(path, str):
            return False
        if os.path.isfile(path):
            return True
        if os.path.isabs(path):
            return False
        return os.path.isfile(os.path.join(settings.BASE_DIR, path))

    @staticmethod
    def bound_path(asset: dict) -> str:
        """Current library file for a session asset that carries `library_id`."""
        if not isinstance(asset, dict):
            return ""
        library_id = str(asset.get("library_id") or "").strip()
        if not library_id:
            return ""
        record = AssetLibrary.get(library_id)
        if not record:
            return ""
        selected = record.get("selected") or ""
        if AssetLibrary.file_exists(selected):
            return selected
        for path in reversed(record.get("versions") or []):
            if path and AssetLibrary.file_exists(path):
                return path
        return ""

    @staticmethod
    def list_assets() -> List[dict]:
        with _LOCK:
            root = AssetLibrary.root()
            if not os.path.isdir(root):
                return []
            records = []
            for name in os.listdir(root):
                if not _ID_RE.match(name):
                    continue
                record = AssetLibrary._read(name)
                if record:
                    records.append(record)
        records.sort(key=lambda item: (item.get("name") or "", item.get("id") or ""))
        return records

    @staticmethod
    def get(asset_id: str) -> Optional[dict]:
        if not _ID_RE.match(asset_id or ""):
            return None
        with _LOCK:
            return AssetLibrary._read(asset_id)

    @staticmethod
    def create_from_file(
        *,
        name: str,
        description: str,
        asset_type: str,
        source_path: str,
    ) -> dict:
        if asset_type not in ASSET_TYPES:
            raise ValueError("asset type must be characters or settings")
        source = AssetLibrary._resolve_existing(source_path)
        ext = AssetLibrary._image_ext(source)
        with _LOCK:
            asset_id = AssetLibrary._new_id()
            directory = AssetLibrary._asset_dir(asset_id)
            os.makedirs(directory, exist_ok=True)
            dest = os.path.join(directory, f"v1{ext}")
            shutil.copyfile(source, dest)
            relative = AssetLibrary._relative(dest)
            record = {
                "id": asset_id,
                "name": name or asset_id,
                "description": description or "",
                "selected": relative,
                "versions": [relative],
                "type": asset_type,
            }
            AssetLibrary._write_record(directory, record)
            return dict(record)

    @staticmethod
    def update(
        asset_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        source_path: Optional[str] = None,
    ) -> dict:
        AssetLibrary._require_id(asset_id)
        with _LOCK:
            record = AssetLibrary._read(asset_id)
            if record is None:
                raise KeyError(asset_id)
            if name is not None:
                record["name"] = name
            if description is not None:
                record["description"] = description
            if source_path:
                source = AssetLibrary._resolve_existing(source_path)
                ext = AssetLibrary._image_ext(source)
                directory = AssetLibrary._asset_dir(asset_id)
                dest = os.path.join(directory, AssetLibrary._next_version_name(directory, ext))
                shutil.copyfile(source, dest)
                relative = AssetLibrary._relative(dest)
                versions = list(record.get("versions") or [])
                if relative not in versions:
                    versions.append(relative)
                record["versions"] = versions
                record["selected"] = relative
            AssetLibrary._write_record(AssetLibrary._asset_dir(asset_id), record)
            return dict(record)

    @staticmethod
    def delete(asset_id: str) -> bool:
        AssetLibrary._require_id(asset_id)
        directory = AssetLibrary._asset_dir(asset_id)
        with _LOCK:
            if not os.path.isdir(directory):
                return False
            shutil.rmtree(directory)
            return True

    @staticmethod
    def as_session_asset(record: dict) -> dict:
        """Placeholder session row. Stage 2 remaps this onto script ids by name."""
        selected = record.get("selected") or ""
        versions = list(record.get("versions") or [])
        return {
            "id": record["id"],
            "name": record.get("name") or "",
            "description": record.get("description") or "",
            "selected": selected,
            "versions": versions,
            "library_id": record["id"],
            "status": "done" if AssetLibrary.file_exists(selected) else "failed",
        }

    @staticmethod
    def _require_id(asset_id: str) -> None:
        if not _ID_RE.match(asset_id or ""):
            raise ValueError("invalid library id")

    @staticmethod
    def _new_id() -> str:
        root = AssetLibrary.root()
        os.makedirs(root, exist_ok=True)
        for _ in range(5):
            candidate = "lib" + uuid.uuid4().hex
            if not os.path.exists(os.path.join(root, candidate)):
                return candidate
        raise RuntimeError("failed to allocate library id")

    @staticmethod
    def _asset_dir(asset_id: str) -> str:
        AssetLibrary._require_id(asset_id)
        directory = os.path.abspath(os.path.join(AssetLibrary.root(), asset_id))
        root = os.path.abspath(AssetLibrary.root())
        if os.path.commonpath([directory, root]) != root:
            raise ValueError("invalid library id")
        return directory

    @staticmethod
    def _read(asset_id: str) -> Optional[dict]:
        path = os.path.join(AssetLibrary.root(), asset_id, "record.json")
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict) or record.get("id") != asset_id:
            return None
        return record

    @staticmethod
    def _write_record(directory: str, record: Dict[str, Any]) -> None:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "record.json")
        tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    @staticmethod
    def _resolve_existing(path: str) -> str:
        if not path or not isinstance(path, str):
            raise ValueError("missing image path")
        if os.path.isfile(path):
            return path
        if not os.path.isabs(path):
            joined = os.path.join(settings.BASE_DIR, path)
            if os.path.isfile(joined):
                return joined
        raise ValueError(f"image not found: {path}")

    @staticmethod
    def _relative(path: str) -> str:
        absolute = os.path.abspath(path)
        base = os.path.abspath(settings.BASE_DIR)
        try:
            relative = os.path.relpath(absolute, base)
        except ValueError:
            return absolute
        if relative.startswith(".."):
            return absolute
        return relative

    @staticmethod
    def _image_ext(path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        if ext not in _IMAGE_EXTS:
            return ".png"
        return ext

    @staticmethod
    def _next_version_name(directory: str, ext: str) -> str:
        max_version = 0
        if os.path.isdir(directory):
            for name in os.listdir(directory):
                stem, _file_ext = os.path.splitext(name)
                match = _VERSION_RE.match(stem)
                if match:
                    max_version = max(max_version, int(match.group(1)))
        return f"v{max_version + 1}{ext}"
