"""Permissioned tool registry and scoped filesystem tools.

A.S.H.R.A is meant to grow into an assistant that can touch apps, sites, and
messages.  That only stays safe if every capability is declared up front with a
risk level and an approval rule.  This module is the single gate:

    read    - inspect notes or files
    draft   - prepare text without sending it
    act     - change local state
    admin   - destructive or configuration-level work

The first tool pack is deliberately read-only and rooted at ``notes/``.  A
write tool is included to prove the approval path, but it cannot run without an
explicit ``approved=True`` from the caller.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .errors import (
    ApprovalRequired,
    PermissionDenied,
    ToolInputError,
    ToolNotFound,
)
from .remember import append as append_capture
from .remember import remember as remember_capture

# Files that must never be exposed through a generic filesystem tool.
BLOCKED_NAMES = {
    ".env",
    ".git",
    ".ssh",
    "config.json",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
}
ALLOWED_SUFFIXES = {
    ".csv",
    ".json",
    ".log",
    ".markdown",
    ".md",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
MAX_READ_BYTES = 256_000


class Permission(str, Enum):
    READ = "read"
    DRAFT = "draft"
    ACT = "act"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _RANKS[self]

    def allows(self, required: "Permission") -> bool:
        return self.rank >= required.rank


_RANKS = {
    Permission.READ: 1,
    Permission.DRAFT: 2,
    Permission.ACT: 3,
    Permission.ADMIN: 4,
}


def parse_permission(value: Permission | str | None) -> Permission:
    if isinstance(value, Permission):
        return value
    try:
        return Permission(str(value or "read").strip().lower())
    except ValueError:
        allowed = ", ".join(item.value for item in Permission)
        raise ToolInputError(f"Unknown permission '{value}'. Use one of: {allowed}.") from None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    handler: Callable[[dict[str, Any]], Any]
    permission: Permission = Permission.READ
    input_schema: dict[str, Any] = field(default_factory=dict)
    requires_approval: bool = False
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "permission": self.permission.value,
            "requires_approval": self.requires_approval,
            "input_schema": self.input_schema,
            "tags": list(self.tags),
        }


class ToolRegistry:
    """Register tools once and invoke them through one permission gate."""

    def __init__(self, audit: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._audit = audit

    def register(self, spec: ToolSpec) -> ToolSpec:
        if not spec.name or not spec.name.strip():
            raise ValueError("A tool needs a name.")
        if spec.name in self._tools:
            raise ValueError(f"Tool '{spec.name}' is already registered.")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(str(name or "").strip())
        if spec is None:
            raise ToolNotFound(f"Tool '{name}' is not registered.")
        return spec

    def list(self, granted: Permission = Permission.READ) -> list[dict[str, Any]]:
        return [
            spec.as_dict()
            for spec in sorted(self._tools.values(), key=lambda item: item.name)
            if granted.allows(spec.permission)
        ]

    def invoke(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        granted: Permission = Permission.READ,
        approved: bool = False,
        request_id: str | None = None,
    ) -> Any:
        spec = self.get(name)
        if not granted.allows(spec.permission):
            raise PermissionDenied(
                f"'{spec.name}' needs {spec.permission.value} permission; "
                f"this caller has {granted.value}."
            )
        if spec.requires_approval and not approved:
            raise ApprovalRequired(
                f"'{spec.name}' changes state and needs explicit approval."
            )

        args = arguments or {}
        _validate_arguments(spec.input_schema, args)

        started = time.perf_counter()
        try:
            result = spec.handler(args)
        except Exception as exc:
            self._record(
                spec,
                args,
                status="error",
                request_id=request_id,
                error=str(exc),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            raise
        self._record(
            spec,
            args,
            status="ok",
            request_id=request_id,
            result=result,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return result

    def _record(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        *,
        status: str,
        request_id: str | None,
        duration_ms: float,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        if self._audit is None:
            return
        payload: dict[str, Any] = {
            "tool": spec.name,
            "permission": spec.permission.value,
            "status": status,
            "request_id": request_id,
            "duration_ms": round(duration_ms, 1),
            "arguments": _preview(arguments),
        }
        if error is not None:
            payload["error"] = error[:1000]
        else:
            payload["result"] = _preview(result)
        try:
            self._audit(payload)
        except Exception:
            # Auditing must never turn a successful tool call into a failure.
            pass


def _preview(value: Any, limit: int = 2000) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        return text[:limit] + "…"
    return value


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "array": (list,),
    "boolean": (bool,),
    "integer": (int,),
    "number": (int, float),
    "object": (dict,),
    "string": (str,),
}


def _validate_arguments(schema: dict[str, Any], arguments: Any) -> None:
    if not isinstance(arguments, dict):
        raise ToolInputError("Tool arguments must be a JSON object.")

    properties = schema.get("properties") if isinstance(schema, dict) else None
    properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else None
    required = required if isinstance(required, list) else []

    for key in required:
        if key not in arguments:
            raise ToolInputError(f"Missing required argument: {key}.")

    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise ToolInputError(f"Unknown argument: {unknown[0]}.")

    for key, value in arguments.items():
        rule = properties.get(key)
        if not isinstance(rule, dict):
            continue

        type_name = rule.get("type")
        if type_name in _JSON_TYPES:
            expected = _JSON_TYPES[type_name]
            if type_name in {"integer", "number"} and isinstance(value, bool):
                raise ToolInputError(f"Argument '{key}' must be a {type_name}.")
            if not isinstance(value, expected):
                raise ToolInputError(f"Argument '{key}' must be a {type_name}.")

        if "enum" in rule and value not in rule["enum"]:
            raise ToolInputError(
                f"Argument '{key}' must be one of: {', '.join(map(str, rule['enum']))}."
            )

        if isinstance(value, str):
            if "minLength" in rule and len(value) < int(rule["minLength"]):
                raise ToolInputError(f"Argument '{key}' is too short.")
            if "maxLength" in rule and len(value) > int(rule["maxLength"]):
                raise ToolInputError(f"Argument '{key}' is too long.")

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in rule and value < rule["minimum"]:
                raise ToolInputError(f"Argument '{key}' is below the minimum.")
            if "maximum" in rule and value > rule["maximum"]:
                raise ToolInputError(f"Argument '{key}' is above the maximum.")


class ScopedFilesystem:
    """Read-only access to one directory tree, with no escape hatch."""

    def __init__(
        self,
        root: Path,
        allowed_suffixes: set[str] | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.allowed_suffixes = {
            suffix.lower() for suffix in (allowed_suffixes or ALLOWED_SUFFIXES)
        }

    # ------------------------------------------------------------------
    # path safety
    # ------------------------------------------------------------------
    def _resolve(self, relative_path: str | None, *, must_exist: bool = False) -> Path:
        raw = str(relative_path or "").strip().replace("\\", "/")
        if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
            raise ToolInputError("Paths must be relative to the notes folder.")

        parts = [part for part in raw.split("/") if part not in {"", "."}]
        if any(part == ".." for part in parts):
            raise ToolInputError("Paths may not leave the notes folder.")
        if any(part.lower() in BLOCKED_NAMES for part in parts):
            raise ToolInputError("That file is not available to tools.")

        candidate = (self.root / Path(*parts)).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolInputError("Paths may not leave the notes folder.")
        if must_exist and not candidate.exists():
            raise ToolInputError("No such file or directory in the notes folder.")
        return candidate

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _inside(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return resolved == self.root or self.root in resolved.parents

    def _is_allowed_file(self, path: Path) -> bool:
        return (
            self._inside(path)
            and path.is_file()
            and path.suffix.lower() in self.allowed_suffixes
            and path.name.lower() not in BLOCKED_NAMES
        )

    # ------------------------------------------------------------------
    # tools
    # ------------------------------------------------------------------
    def list_directory(
        self,
        relative_path: str = "",
        recursive: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        directory = self._resolve(relative_path, must_exist=True)
        if not directory.is_dir():
            raise ToolInputError("That path is not a directory.")

        limit = max(1, min(1000, int(limit)))
        iterator = directory.rglob("*") if recursive else directory.iterdir()
        entries: list[dict[str, Any]] = []
        truncated = False

        for item in sorted(iterator, key=lambda path: path.as_posix().lower()):
            if not self._inside(item):
                continue
            if any(part.lower() in BLOCKED_NAMES for part in item.relative_to(self.root).parts):
                continue
            if item.is_dir():
                entries.append(
                    {
                        "path": self._relative(item),
                        "name": item.name,
                        "type": "directory",
                    }
                )
            elif self._is_allowed_file(item):
                try:
                    size = item.stat().st_size
                except OSError:
                    size = None
                entries.append(
                    {
                        "path": self._relative(item),
                        "name": item.name,
                        "type": "file",
                        "size": size,
                    }
                )
            if len(entries) >= limit:
                truncated = True
                break

        return {
            "root": str(self.root),
            "path": self._relative(directory) if directory != self.root else "",
            "entries": entries,
            "truncated": truncated,
        }

    def read_text(self, relative_path: str, max_chars: int = 20000) -> dict[str, Any]:
        path = self._resolve(relative_path, must_exist=True)
        if not self._is_allowed_file(path):
            raise ToolInputError("That file type is not readable by tools.")

        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ToolInputError(f"Could not read that file: {exc}") from None
        if size > MAX_READ_BYTES:
            raise ToolInputError(
                f"That file is too large for a tool read ({size:,} bytes)."
            )

        max_chars = max(100, min(100_000, int(max_chars)))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolInputError(f"Could not read that file: {exc}") from None

        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]

        return {
            "path": self._relative(path),
            "content": text,
            "chars": len(text),
            "truncated": truncated,
        }

    def search(
        self,
        query: str,
        limit: int = 20,
        excerpt_chars: int = 200,
    ) -> dict[str, Any]:
        needle = " ".join(str(query or "").split())
        if len(needle) < 2:
            raise ToolInputError("Search for at least two characters.")

        limit = max(1, min(100, int(limit)))
        excerpt_chars = max(40, min(500, int(excerpt_chars)))
        lowered = needle.lower()
        results: list[dict[str, Any]] = []
        truncated = False

        if not self.root.is_dir():
            return {"query": needle, "results": results, "truncated": False}

        for path in sorted(self.root.rglob("*"), key=lambda item: item.as_posix().lower()):
            if not self._is_allowed_file(path):
                continue
            try:
                if path.stat().st_size > MAX_READ_BYTES:
                    continue
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue

            for number, line in enumerate(lines, start=1):
                if lowered not in line.lower():
                    continue
                excerpt = " ".join(line.split())
                if len(excerpt) > excerpt_chars:
                    excerpt = excerpt[:excerpt_chars].rstrip() + "…"
                results.append(
                    {
                        "path": self._relative(path),
                        "line": number,
                        "excerpt": excerpt,
                    }
                )
                if len(results) >= limit:
                    truncated = True
                    break
            if truncated:
                break

        return {"query": needle, "results": results, "truncated": truncated}


def _fs_list(fs: ScopedFilesystem) -> Callable[[dict[str, Any]], Any]:
    def handler(args: dict[str, Any]) -> Any:
        return fs.list_directory(
            relative_path=args.get("relative_path", ""),
            recursive=bool(args.get("recursive", False)),
            limit=int(args.get("limit", 200)),
        )

    return handler


def _fs_read(fs: ScopedFilesystem) -> Callable[[dict[str, Any]], Any]:
    def handler(args: dict[str, Any]) -> Any:
        return fs.read_text(
            relative_path=str(args.get("relative_path", "")),
            max_chars=int(args.get("max_chars", 20000)),
        )

    return handler


def _fs_search(fs: ScopedFilesystem) -> Callable[[dict[str, Any]], Any]:
    def handler(args: dict[str, Any]) -> Any:
        return fs.search(
            query=str(args.get("query", "")),
            limit=int(args.get("limit", 20)),
            excerpt_chars=int(args.get("excerpt_chars", 200)),
        )

    return handler


def _vault_remember(base_dir: Path) -> Callable[[dict[str, Any]], Any]:
    def handler(args: dict[str, Any]) -> Any:
        result = remember_capture(
            base_dir,
            str(args.get("text", "")),
            source=str(args.get("source", "typed")),
        )
        return {
            "title": result.title,
            "path": result.path,
            "node": result.node,
            "related": result.related,
            "reply": result.reply,
        }

    return handler


def _vault_append(base_dir: Path) -> Callable[[dict[str, Any]], Any]:
    def handler(args: dict[str, Any]) -> Any:
        path = args.get("path")
        last = bool(args.get("last", False))
        if not path and not last:
            raise ToolInputError("Name a capture path, or set last=true.")
        result = append_capture(
            base_dir,
            str(args.get("text", "")),
            path=str(path) if path else None,
            last=last,
        )
        return {
            "title": result.title,
            "path": result.path,
            "node": result.node,
            "reply": result.reply,
        }

    return handler


def build_default_registry(
    base_dir: Path,
    *,
    fs_root: Path | None = None,
    audit: Callable[[dict[str, Any]], None] | None = None,
) -> ToolRegistry:
    """The local-first tool pack: scoped reads plus approval-gated writes."""
    base_dir = Path(base_dir)
    fs = ScopedFilesystem(fs_root or base_dir / "notes")
    registry = ToolRegistry(audit=audit)

    registry.register(
        ToolSpec(
            name="fs_list",
            description="List files and folders inside the notes vault.",
            permission=Permission.READ,
            input_schema={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "default": ""},
                    "recursive": {"type": "boolean", "default": False},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1000,
                        "default": 200,
                    },
                },
                "additionalProperties": False,
            },
            handler=_fs_list(fs),
            tags=("filesystem", "notes"),
        )
    )
    registry.register(
        ToolSpec(
            name="fs_read",
            description="Read a text or markdown file inside the notes vault.",
            permission=Permission.READ,
            input_schema={
                "type": "object",
                "properties": {
                    "relative_path": {"type": "string", "minLength": 1},
                    "max_chars": {
                        "type": "integer",
                        "minimum": 100,
                        "maximum": 100000,
                        "default": 20000,
                    },
                },
                "required": ["relative_path"],
                "additionalProperties": False,
            },
            handler=_fs_read(fs),
            tags=("filesystem", "notes"),
        )
    )
    registry.register(
        ToolSpec(
            name="fs_search",
            description="Search notes for a phrase and return matching lines.",
            permission=Permission.READ,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 2},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 20,
                    },
                    "excerpt_chars": {
                        "type": "integer",
                        "minimum": 40,
                        "maximum": 500,
                        "default": 200,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            handler=_fs_search(fs),
            tags=("filesystem", "notes", "search"),
        )
    )
    registry.register(
        ToolSpec(
            name="vault_remember",
            description="Write a new capture into the vault. Requires approval.",
            permission=Permission.ACT,
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 3, "maxLength": 2000},
                    "source": {
                        "type": "string",
                        "enum": ["typed", "voice"],
                        "default": "typed",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=_vault_remember(base_dir),
            requires_approval=True,
            tags=("vault", "write"),
        )
    )
    registry.register(
        ToolSpec(
            name="vault_append",
            description="Append a paragraph to an existing capture. Requires approval.",
            permission=Permission.ACT,
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "minLength": 3, "maxLength": 2000},
                    "path": {"type": "string"},
                    "last": {"type": "boolean", "default": False},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
            handler=_vault_append(base_dir),
            requires_approval=True,
            tags=("vault", "write"),
        )
    )
    return registry
