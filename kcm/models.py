"""Validated, version-independent printer and component profiles."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
import heapq
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping
from urllib.parse import urlsplit


_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_CAN_ID = re.compile(r"[0-9a-fA-F]{12}\Z")
_INTERFACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}\Z")
_SERVICE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./-]{0,255}\Z")
_DEVICE = re.compile(r"/dev/[A-Za-z0-9_./:+-]+\Z")


def validate_id(value: str, label: str = "ID") -> None:
    """Require a short slug that is also safe as a path segment."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} must use lowercase letters, digits, '-' or '_' (1–64 characters)")


def _text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError(f"{label} must be non-empty text without control characters")


def _absolute_posix(value: str, label: str) -> None:
    _text(value, label)
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"{label} must be an absolute Linux path without '..'")


def _device(value: str, label: str) -> None:
    if not isinstance(value, str) or not _DEVICE.fullmatch(value) or ".." in PurePosixPath(value).parts:
        raise ValueError(f"{label} must be an absolute /dev/ path without '..'")


def _construct(cls: type, data: Mapping[str, Any]) -> Any:
    if not isinstance(data, Mapping):
        raise ValueError(f"{cls.__name__} must be an object")
    allowed = {f.name for f in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} fields: {', '.join(sorted(str(k) for k in unknown))}")
    try:
        result = cls(**dict(data))
    except TypeError as exc:
        raise ValueError(f"Invalid {cls.__name__} fields: {exc}") from exc
    result.validate()
    return result


@dataclass
class Component:
    id: str
    name: str
    config_file: str
    transport: str
    can_interface: str = "can0"
    can_uuid: str = ""
    application_device: str = ""
    flash_device: str = ""
    baud: int = 250000
    bootloader_entry: str = "auto"
    via: str = ""
    application_baud: int = 250000

    def validate(self) -> None:
        validate_id(self.id, "Component ID")
        _text(self.name, "Component name")
        _text(self.config_file, "Build configuration path")
        # Accept native absolute paths as well, so profiles can be prepared and
        # unit-tested on another OS before being installed on the Linux host.
        if not (Path(self.config_file).is_absolute() or PurePosixPath(self.config_file).is_absolute()):
            raise ValueError("Build configuration path must be absolute")
        if ".." in PurePosixPath(self.config_file.replace("\\", "/")).parts:
            raise ValueError("Build configuration path must not contain '..'")
        if self.transport not in ("can", "usb", "uart"):
            raise ValueError("Transport must be can, usb, or uart")
        if self.bootloader_entry not in ("auto", "manual", "already"):
            raise ValueError("Bootloader entry must be auto, manual, or already")
        if not isinstance(self.baud, int) or isinstance(self.baud, bool) or not 1200 <= self.baud <= 4_000_000:
            raise ValueError("UART baud rate must be an integer between 1200 and 4000000")
        if not isinstance(self.application_baud, int) or isinstance(self.application_baud, bool) or not 1200 <= self.application_baud <= 4_000_000:
            raise ValueError("Application UART baud rate must be an integer between 1200 and 4000000")
        if not isinstance(self.can_interface, str) or not _INTERFACE.fullmatch(self.can_interface):
            raise ValueError("CAN interface must be a valid Linux interface name (maximum 15 characters)")
        if not isinstance(self.can_uuid, str):
            raise ValueError("CAN UUID must be text")
        if self.can_uuid and not _CAN_ID.fullmatch(self.can_uuid):
            raise ValueError("CAN UUID must contain exactly 12 hexadecimal characters")
        if self.transport == "can":
            if not self.can_uuid:
                raise ValueError("CAN components require a CAN UUID")
            if self.application_device or self.flash_device:
                raise ValueError("CAN flashing uses a UUID; use USB or UART transport for a bridge flashed by serial")
        else:
            _device(self.flash_device, "Flashing device")
            if self.application_device:
                _device(self.application_device, "Application device")
            elif self.bootloader_entry == "auto" and not self.can_uuid:
                raise ValueError("Automatic bootloader entry requires an application device or a bridge CAN UUID")
        if not isinstance(self.via, str):
            raise ValueError("Upstream bridge ID must be text")
        if self.via:
            validate_id(self.via, "Upstream bridge ID")
            if self.via == self.id:
                raise ValueError("A component cannot use itself as its upstream bridge")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Component:
        return _construct(cls, data)


@dataclass
class Printer:
    id: str
    name: str
    klipper_path: str
    katapult_path: str
    service: str = "klipper"
    moonraker_url: str = "http://127.0.0.1:7125"
    source_ref: str = "origin/master"
    components: list[Component] = field(default_factory=list)

    def validate(self) -> None:
        validate_id(self.id, "Printer ID")
        _text(self.name, "Printer name")
        _absolute_posix(self.klipper_path, "Klipper path")
        _absolute_posix(self.katapult_path, "Katapult path")
        if not isinstance(self.service, str) or not _SERVICE.fullmatch(self.service):
            raise ValueError("Klipper service must be a systemd service name")
        _text(self.moonraker_url, "Moonraker URL")
        try:
            address = urlsplit(self.moonraker_url)
            valid_url = (address.scheme in ("http", "https") and address.hostname
                         and address.port != 0 and not address.username and not address.password
                         and not address.query and not address.fragment)
        except ValueError as exc:
            raise ValueError("Moonraker URL is invalid") from exc
        if not valid_url:
            raise ValueError("Moonraker URL must be an HTTP(S) URL without credentials, query, or fragment")
        if not isinstance(self.source_ref, str) or not _REF.fullmatch(self.source_ref):
            raise ValueError("Source reference must be a branch, tag, or commit, without spaces or leading '-'")
        if not isinstance(self.components, list) or any(not isinstance(c, Component) for c in self.components):
            raise ValueError("Components must be a list of Component profiles")
        ids: set[str] = set()
        can_targets: set[tuple[str, str]] = set()
        flash_targets: set[str] = set()
        application_targets: set[str] = set()
        for component in self.components:
            component.validate()
            if component.id in ids:
                raise ValueError(f"Duplicate component ID: {component.id}")
            ids.add(component.id)
            if component.can_uuid:
                target = (component.can_interface, component.can_uuid.lower())
                if target in can_targets:
                    raise ValueError(f"Duplicate CAN target: {target[0]} / {target[1]}")
                can_targets.add(target)
            if component.flash_device:
                if component.flash_device in flash_targets:
                    raise ValueError(f"Duplicate flashing device: {component.flash_device}")
                flash_targets.add(component.flash_device)
            if component.application_device:
                if component.application_device in application_targets:
                    raise ValueError(f"Duplicate application device: {component.application_device}")
                application_targets.add(component.application_device)
        self._ordered_components()

    def _ordered_components(self) -> list[Component]:
        by_id = {component.id: index for index, component in enumerate(self.components)}
        incoming = [0] * len(self.components)
        outgoing: list[list[int]] = [[] for _ in self.components]
        for index, component in enumerate(self.components):
            if component.via:
                if component.via not in by_id:
                    raise ValueError(f"Unknown upstream bridge '{component.via}' for '{component.id}'")
                parent = by_id[component.via]
                outgoing[index].append(parent)
                incoming[parent] += 1
        ready = [index for index, count in enumerate(incoming) if count == 0]
        heapq.heapify(ready)
        ordered: list[Component] = []
        while ready:
            index = heapq.heappop(ready)
            ordered.append(self.components[index])
            for parent in outgoing[index]:
                incoming[parent] -= 1
                if incoming[parent] == 0:
                    heapq.heappush(ready, parent)
        if len(ordered) != len(self.components):
            raise ValueError("Upstream bridge relationships contain a cycle")
        return ordered

    def ordered_components(self) -> list[Component]:
        """Return stable flash order: downstream devices before their bridges."""
        self.validate()
        return self._ordered_components()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Printer:
        if not isinstance(data, Mapping):
            raise ValueError("Printer must be an object")
        values = dict(data)
        components = values.get("components", [])
        if not isinstance(components, list):
            raise ValueError("Components must be a list")
        values["components"] = [Component.from_dict(component) for component in components]
        return _construct(cls, values)
