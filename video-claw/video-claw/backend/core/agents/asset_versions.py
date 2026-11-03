# -*- coding: utf-8 -*-
"""Version files named ``{id}.ext`` or ``{id}_v<digits>.ext``.

A longer id such as ``char_10`` is not a version of ``char_1``.
"""

import os
from typing import Iterable, List


class AssetVersions:
    """Find version files without treating a longer id as a version of a shorter one."""

    @staticmethod
    def matches(asset_id: str, filename: str, extensions: Iterable[str]) -> bool:
        """True when ``filename`` is exactly ``asset_id`` or ``asset_id_v<digits>``."""
        if not asset_id:
            return False
        base = os.path.basename(filename)
        stem, ext = os.path.splitext(base)
        allowed = {item.lower().lstrip(".") for item in extensions}
        if ext.lower().lstrip(".") not in allowed:
            return False
        if stem == asset_id:
            return True
        prefix = f"{asset_id}_v"
        if not stem.startswith(prefix):
            return False
        version = stem[len(prefix):]
        return version.isdigit()

    @staticmethod
    def list_in(directory: str, asset_id: str, extensions: Iterable[str]) -> List[str]:
        """Return matching files in ``directory``, oldest first."""
        if not asset_id or not directory or not os.path.isdir(directory):
            return []
        paths = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if AssetVersions.matches(asset_id, name, extensions)
        ]
        return sorted(paths, key=lambda path: (os.path.getmtime(path), os.path.basename(path)))
