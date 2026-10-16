"""按 sha256 保存字节。同一份文件只写一次。"""

from __future__ import annotations

import hashlib
import os
import re

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ArtifactError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def is_checksum(value: str) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


class ArtifactStore:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def put(self, data: bytes) -> str:
        if not isinstance(data, bytes) or not data:
            raise ArtifactError("空文件。")
        digest = hashlib.sha256(data).hexdigest()
        target = self.path_for(digest)
        if os.path.isfile(target):
            return digest
        os.makedirs(os.path.dirname(target), exist_ok=True)
        temporary = target + ".part"
        try:
            with open(temporary, "wb") as handle:
                handle.write(data)
            os.replace(temporary, target)
        except OSError as exc:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise ArtifactError("没能留下这个文件。") from exc
        return digest

    def get(self, checksum: str) -> bytes | None:
        path = self.path_for(checksum)
        if path is None:
            return None
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def path_for(self, checksum: str) -> str | None:
        if not is_checksum(checksum):
            return None
        return os.path.join(self.root, "blobs", checksum[:2], checksum)
