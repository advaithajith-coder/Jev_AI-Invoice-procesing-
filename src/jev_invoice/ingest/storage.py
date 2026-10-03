"""Content-addressed storage for original uploads.

Files are written once under `<root>/<sha[:2]>/<sha><ext>`; writing the same bytes
again is a no-op. Swapping this for object storage (S3, GCS) means implementing the
same two methods.
"""

import os
import tempfile
from pathlib import Path


class LocalFileStorage:
    def __init__(self, root: Path) -> None:
        self._root = root

    def save(self, data: bytes, *, sha256: str, extension: str) -> str:
        """Store *data* and return its storage key (a relative path)."""
        key = f"{sha256[:2]}/{sha256}{extension}"
        path = self._root / key
        if path.exists():
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".upload-")
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
            os.replace(tmp_name, path)  # atomic: readers never see a partial file
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return key

    def read(self, key: str) -> bytes:
        path = (self._root / key).resolve()
        if self._root.resolve() not in path.parents:
            raise ValueError("storage key escapes the storage root")
        return path.read_bytes()
