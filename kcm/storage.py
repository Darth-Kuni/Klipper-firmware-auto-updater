"""Atomic, versioned profile storage. No external services are contacted."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .models import Printer, validate_id


SCHEMA_VERSION = 1


def atomic_json(path: Path, data: Any) -> None:
    """Replace a JSON file atomically, retaining the old file on write failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        # Make the rename durable where directory fsync is available (Linux).
        if hasattr(os, "O_DIRECTORY"):
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Store:
    """A registry plus one saved Kconfig file per printer component."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.registry_path = self.root / "registry.json"

    def load(self) -> list[Printer]:
        if not self.registry_path.exists():
            return []
        try:
            with self.registry_path.open(encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot read profile registry {self.registry_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("Profile registry must be an object")
        if type(data.get("schema_version")) is not int or data["schema_version"] != SCHEMA_VERSION:
            raise ValueError(f"Unsupported profile schema version: {data.get('schema_version')!r}")
        if set(data) != {"schema_version", "printers"}:
            raise ValueError("Profile registry contains unexpected or missing fields")
        if not isinstance(data["printers"], list):
            raise ValueError("Profile registry printers must be a list")
        printers = [Printer.from_dict(printer) for printer in data["printers"]]
        self._validate_printers(printers)
        return printers

    @staticmethod
    def _validate_printers(printers: list[Printer]) -> None:
        if not isinstance(printers, list) or any(not isinstance(p, Printer) for p in printers):
            raise ValueError("Printers must be a list of Printer profiles")
        ids: set[str] = set()
        for printer in printers:
            printer.validate()
            if printer.id in ids:
                raise ValueError(f"Duplicate printer ID: {printer.id}")
            ids.add(printer.id)

    def save(self, printers: list[Printer]) -> None:
        self._validate_printers(printers)
        atomic_json(self.registry_path, {"schema_version": SCHEMA_VERSION,
                                         "printers": [printer.to_dict() for printer in printers]})

    def get(self, printer_id: str) -> Printer:
        validate_id(printer_id, "Printer ID")
        for printer in self.load():
            if printer.id == printer_id:
                return printer
        raise KeyError(f"No saved printer with ID '{printer_id}'")

    def config_path(self, printer_id: str, component_id: str) -> Path:
        validate_id(printer_id, "Printer ID")
        validate_id(component_id, "Component ID")
        path = self.root / "profiles" / printer_id / f"{component_id}.config"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path
