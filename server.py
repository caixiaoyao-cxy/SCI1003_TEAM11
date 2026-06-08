from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import shlex
import shutil
import stat
import string
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
import uuid
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlparse

import yaml
from mcp.types import ImageContent, TextContent

try:
    from fastmcp import FastMCP
except ImportError:  # Compatibility with older MCP Python SDK layouts.
    from mcp.server.fastmcp import FastMCP


APP_ROOT = Path(os.environ.get("MCP_APP_ROOT", Path(__file__).resolve().parent)).resolve()
DATA_ROOT = Path(os.environ.get("MCP_DATA_ROOT", "/data")).resolve()
REGISTRY_PATH = Path(os.environ.get("MCP_REGISTRY_PATH", APP_ROOT / "registry.yaml")).resolve()
USER_REGISTRY_PATH = Path(
    os.environ.get("MCP_USER_REGISTRY_PATH", DATA_ROOT / "mcp_registry.yaml")
).resolve()
TOOL_BIN_ROOT = Path(os.environ.get("MCP_TOOL_BIN_ROOT", DATA_ROOT / "mcp_tools" / "bin")).resolve()
TOOL_VENV_ROOT = Path(
    os.environ.get("MCP_TOOL_VENV_ROOT", DATA_ROOT / "mcp_tools" / "venvs")
).resolve()
TOOL_JAVA_ROOT = Path(
    os.environ.get("MCP_TOOL_JAVA_ROOT", DATA_ROOT / "mcp_tools" / "java")
).resolve()
STORAGE_ROOT = APP_ROOT / "storage"
OUTPUT_ROOT = DATA_ROOT / "mcp_outputs"
os.environ["PATH"] = f"{TOOL_BIN_ROOT}{os.pathsep}{os.environ.get('PATH', '')}"

VALID_TOOL_TYPES = {"cli", "java", "script"}
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
APT_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:-]*$")
SAFE_BINARY_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
DEFAULT_TIMEOUT_SECONDS = 60 * 60

logging.basicConfig(
    level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("bio-mcp-server")

mcp = FastMCP("secure-bioinformatics-platform")
registry_cache: dict[str, dict[str, Any]] = {}
registered_tool_names: set[str] = set()
INSTALL_LOCK = threading.RLock()
REGISTRY_LOCK = threading.RLock()


class RegistryError(ValueError):
    """Raised when registry configuration is malformed."""


class InstallConflictError(RuntimeError):
    """Raised when a dynamic tool install would overwrite existing state."""


def ensure_runtime_directories() -> None:
    if not is_relative_to(TOOL_BIN_ROOT, DATA_ROOT):
        raise RuntimeError(f"MCP_TOOL_BIN_ROOT must resolve inside {DATA_ROOT}; got {TOOL_BIN_ROOT}")
    if not is_relative_to(TOOL_VENV_ROOT, DATA_ROOT):
        raise RuntimeError(f"MCP_TOOL_VENV_ROOT must resolve inside {DATA_ROOT}; got {TOOL_VENV_ROOT}")
    if not is_relative_to(TOOL_JAVA_ROOT, DATA_ROOT):
        raise RuntimeError(f"MCP_TOOL_JAVA_ROOT must resolve inside {DATA_ROOT}; got {TOOL_JAVA_ROOT}")
    for directory in (
        APP_ROOT,
        STORAGE_ROOT,
        DATA_ROOT,
        OUTPUT_ROOT,
        TOOL_BIN_ROOT,
        TOOL_VENV_ROOT,
        TOOL_JAVA_ROOT,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        tmp_path = Path(handle.name)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def load_yaml_registry(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise RegistryError(f"Registry file does not exist: {path}")
        return {"tools": []}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RegistryError(f"Unable to parse registry YAML at {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RegistryError(f"Registry root must be a mapping: {path}")
    return loaded


def load_registry_file() -> dict[str, Any]:
    return load_yaml_registry(REGISTRY_PATH, required=True)


def load_user_registry_file() -> dict[str, Any]:
    return load_yaml_registry(USER_REGISTRY_PATH, required=False)


def load_effective_registry() -> dict[str, Any]:
    base_registry = load_registry_file()
    user_registry = load_user_registry_file()

    base_tools = base_registry.get("tools", [])
    user_tools = user_registry.get("tools", [])
    if not isinstance(base_tools, list):
        raise RegistryError(f"Base registry key 'tools' must be a list: {REGISTRY_PATH}")
    if not isinstance(user_tools, list):
        raise RegistryError(f"User registry key 'tools' must be a list: {USER_REGISTRY_PATH}")

    effective = dict(base_registry)
    effective["tools"] = [*base_tools, *user_tools]
    return effective


def validate_input_name(name: str) -> None:
    if not IDENTIFIER_RE.match(name):
        raise RegistryError(
            f"Input name '{name}' is invalid. Use a Python identifier such as sample_bam_path."
        )


def validate_tool_spec(spec: dict[str, Any], existing_names: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise RegistryError("Each tool entry must be a mapping.")

    name = spec.get("name")
    tool_type = spec.get("type")
    command = spec.get("command")
    description = spec.get("description", "")
    inputs = spec.get("inputs", {})
    output_control = spec.get("output_control", {})
    expected_outputs = spec.get("expected_outputs", [])

    if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
        raise RegistryError(f"Invalid tool name: {name!r}")
    if existing_names is not None and name in existing_names:
        raise RegistryError(f"Duplicate tool name: {name}")
    if tool_type not in VALID_TOOL_TYPES:
        raise RegistryError(f"Tool '{name}' has unsupported type: {tool_type!r}")
    if not isinstance(command, str) or not command.strip():
        raise RegistryError(f"Tool '{name}' must define a non-empty command string.")
    if not isinstance(description, str):
        raise RegistryError(f"Tool '{name}' description must be a string.")
    if not isinstance(inputs, dict):
        raise RegistryError(f"Tool '{name}' inputs must be a mapping.")
    for input_name, input_schema in inputs.items():
        validate_input_name(input_name)
        if not isinstance(input_schema, dict):
            raise RegistryError(f"Tool '{name}' input '{input_name}' must be a mapping.")
    if not isinstance(output_control, dict):
        raise RegistryError(f"Tool '{name}' output_control must be a mapping.")
    if not isinstance(expected_outputs, list):
        raise RegistryError(f"Tool '{name}' expected_outputs must be a list.")
    for output in expected_outputs:
        if not isinstance(output, dict) or not isinstance(output.get("name"), str):
            raise RegistryError(f"Tool '{name}' has an invalid expected_outputs entry.")

    placeholders = {
        field_name.split(".", 1)[0].split("[", 1)[0]
        for _, field_name, _, _ in string.Formatter().parse(command)
        if field_name
    }
    missing_placeholders = sorted(placeholders - set(inputs))
    if missing_placeholders:
        raise RegistryError(
            f"Tool '{name}' command references undefined inputs: {missing_placeholders}"
        )

    normalized = dict(spec)
    normalized["description"] = description
    normalized["inputs"] = inputs
    normalized["output_control"] = {
        "intercept_large_output": bool(output_control.get("intercept_large_output", False)),
        "max_preview_lines": int(output_control.get("max_preview_lines", 100)),
    }
    normalized["expected_outputs"] = expected_outputs
    normalized["timeout_seconds"] = int(spec.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    return normalized


def validate_registry(registry: dict[str, Any]) -> list[dict[str, Any]]:
    tools = registry.get("tools", [])
    if not isinstance(tools, list):
        raise RegistryError("Registry key 'tools' must be a list.")
    seen: set[str] = set()
    normalized_tools: list[dict[str, Any]] = []
    for spec in tools:
        normalized = validate_tool_spec(spec, seen)
        seen.add(normalized["name"])
        normalized_tools.append(normalized)
    return normalized_tools


def looks_like_path_param(name: str, schema: dict[str, Any]) -> bool:
    lowered = name.lower()
    schema_type = str(schema.get("type", "")).lower()
    return (
        schema_type in {"path", "file", "filepath"}
        or "path" in lowered
        or lowered.endswith(("_file", "_dir", "_jar", "_bam", "_bed", "_fq", "_fasta"))
    )


def configured_host_data_roots() -> list[PureWindowsPath]:
    raw_roots: list[str] = []
    single_root = os.environ.get("MCP_HOST_DATA_ROOT", "").strip()
    if single_root:
        raw_roots.append(single_root)
    multi_roots = os.environ.get("MCP_HOST_DATA_ROOTS", "")
    raw_roots.extend(root.strip() for root in multi_roots.split(";") if root.strip())
    return [PureWindowsPath(root) for root in raw_roots]


def windows_parts_match_prefix(parts: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    if len(parts) < len(prefix):
        return False
    return all(left.casefold() == right.casefold() for left, right in zip(parts, prefix))


def translate_windows_host_path(raw: str, param_name: str) -> Path | None:
    if not WINDOWS_ABSOLUTE_PATH_RE.match(raw):
        return None

    windows_path = PureWindowsPath(raw)
    host_roots = configured_host_data_roots()
    if not host_roots:
        raise ValueError(
            f"Path parameter '{param_name}' is a Windows host path. Pass a /data path, "
            "or configure MCP_HOST_DATA_ROOT to the host directory mounted as /data."
        )

    for host_root in host_roots:
        if windows_parts_match_prefix(windows_path.parts, host_root.parts):
            relative_parts = windows_path.parts[len(host_root.parts) :]
            return DATA_ROOT.joinpath(*relative_parts)

    roots_text = ", ".join(str(root) for root in host_roots)
    raise ValueError(
        f"Path parameter '{param_name}' is outside configured host data root(s): {roots_text}. "
        f"Use a path under the mounted data directory or pass a /data path; got {raw}"
    )


def normalize_data_path(value: Any, param_name: str, create_parent: bool = False) -> str:
    raw = str(value)
    translated = translate_windows_host_path(raw, param_name)
    candidate = translated if translated is not None else Path(raw)
    if not candidate.is_absolute():
        candidate = DATA_ROOT / candidate
    resolved = candidate.resolve(strict=False)
    if not is_relative_to(resolved, DATA_ROOT):
        raise ValueError(
            f"Path parameter '{param_name}' must resolve inside {DATA_ROOT}; got {resolved}"
        )
    if create_parent:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    return str(resolved)


def sanitize_command_arguments(spec: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    inputs = spec.get("inputs", {})
    sanitized: dict[str, Any] = {}
    for input_name, input_schema in inputs.items():
        if input_name not in arguments:
            raise ValueError(f"Missing required input: {input_name}")
        value = arguments[input_name]
        if looks_like_path_param(input_name, input_schema):
            lowered = input_name.lower()
            create_parent = any(token in lowered for token in ("output", "out", "target", "report"))
            sanitized[input_name] = normalize_data_path(value, input_name, create_parent=create_parent)
        else:
            sanitized[input_name] = str(value)
    return sanitized


def render_command(command_template: str, arguments: dict[str, Any]) -> list[str]:
    quoted_args = {key: shlex.quote(str(value)) for key, value in arguments.items()}
    rendered = command_template.format(**quoted_args)
    try:
        argv = shlex.split(rendered, posix=True)
    except ValueError as exc:
        raise ValueError(f"Unable to split command safely: {exc}") from exc
    if not argv:
        raise ValueError("Rendered command is empty.")
    return argv


def firewall_text(
    text: str,
    channel: str,
    control: dict[str, Any],
    force_intercept: bool = False,
) -> dict[str, Any]:
    lines = text.splitlines()
    max_preview_lines = max(0, int(control.get("max_preview_lines", 100)))
    should_intercept = bool(control.get("intercept_large_output", False)) or force_intercept
    is_large = should_intercept and len(lines) > max_preview_lines

    result: dict[str, Any] = {
        "channel": channel,
        "total_lines": len(lines),
        "intercepted": is_large,
    }
    if is_large:
        output_path = OUTPUT_ROOT / f"bio_output_{uuid.uuid4().hex}.txt"
        atomic_write_text(output_path, text)
        result.update(
            {
                "preview_lines": max_preview_lines,
                "preview": "\n".join(lines[:max_preview_lines]),
                "full_output_path": str(output_path),
            }
        )
    else:
        result.update({"preview_lines": len(lines), "text": text})
    return result


def image_content_from_path(path: Path) -> ImageContent:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return ImageContent(type="image", data=encoded, mimeType=mime_type)


def text_content(text: str) -> TextContent:
    return TextContent(type="text", text=text)


def render_dataframe(path: Path, max_rows: int) -> str:
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas is required to render tabular outputs.") from exc

    suffix = path.suffix.lower()
    sep = "\t" if suffix in {".tsv", ".tab", ".txt"} else ","
    df = pd.read_csv(path, sep=sep, nrows=max_rows)
    if df.empty:
        return f"### {path.name}\n\n_No rows found._"
    return f"### {path.name}\n\n{df.to_markdown(index=False)}"


def render_pdf_summary(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is required to render PDF summaries.") from exc

    reader = PdfReader(str(path))
    summaries: list[str] = []
    for index, page in enumerate(reader.pages[:3], start=1):
        text = (page.extract_text() or "").strip()
        if text:
            summaries.append(f"### {path.name} page {index}\n\n{text[:4000]}")
        else:
            summaries.append(f"### {path.name} page {index}\n\n_No extractable text found._")
    return "\n\n".join(summaries) if summaries else f"### {path.name}\n\n_No pages found._"


def render_text_file(path: Path, max_lines: int) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    protected = firewall_text(
        text,
        channel=f"file:{path.name}",
        control={"intercept_large_output": True, "max_preview_lines": max_lines},
        force_intercept=True,
    )
    return json.dumps(protected, indent=2)


def render_expected_outputs(
    spec: dict[str, Any],
    arguments: dict[str, Any],
) -> tuple[list[Any], list[dict[str, Any]]]:
    content_blocks: list[Any] = []
    output_metadata: list[dict[str, Any]] = []

    for output in spec.get("expected_outputs", []):
        output_name = output["name"]
        render_as = str(output.get("render_as", "auto")).lower()
        max_rows = int(output.get("max_rows", 20))
        max_lines = int(output.get("max_preview_lines", spec["output_control"]["max_preview_lines"]))

        raw_path = arguments.get(output_name) or output.get("path")
        if raw_path is None:
            output_metadata.append(
                {"name": output_name, "status": "missing_path", "render_as": render_as}
            )
            continue

        try:
            output_path = Path(normalize_data_path(raw_path, output_name))
        except ValueError as exc:
            output_metadata.append(
                {
                    "name": output_name,
                    "status": "blocked_by_data_firewall",
                    "reason": str(exc),
                    "render_as": render_as,
                }
            )
            continue

        if not output_path.exists():
            output_metadata.append(
                {
                    "name": output_name,
                    "status": "not_found",
                    "path": str(output_path),
                    "render_as": render_as,
                }
            )
            continue

        if render_as == "auto":
            suffix = output_path.suffix.lower()
            if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                render_as = "image"
            elif suffix in {".csv", ".tsv", ".tab"}:
                render_as = "dataframe"
            elif suffix == ".pdf":
                render_as = "pdf"
            else:
                render_as = "text"

        try:
            if render_as == "image":
                content_blocks.append(image_content_from_path(output_path))
            elif render_as in {"dataframe", "table", "csv", "tsv"}:
                content_blocks.append(text_content(render_dataframe(output_path, max_rows=max_rows)))
            elif render_as == "pdf":
                content_blocks.append(text_content(render_pdf_summary(output_path)))
            else:
                content_blocks.append(text_content(render_text_file(output_path, max_lines=max_lines)))
            output_metadata.append(
                {
                    "name": output_name,
                    "status": "rendered",
                    "path": str(output_path),
                    "render_as": render_as,
                }
            )
        except Exception as exc:  # Keep dynamic tools usable even when one artifact fails.
            logger.exception("Failed rendering expected output %s for tool %s", output_name, spec["name"])
            output_metadata.append(
                {
                    "name": output_name,
                    "status": "render_error",
                    "path": str(output_path),
                    "render_as": render_as,
                    "reason": str(exc),
                }
            )

    return content_blocks, output_metadata


def execute_registered_tool(tool_name: str, arguments: dict[str, Any]) -> list[Any]:
    if tool_name not in registry_cache:
        return [
            text_content(
                json.dumps({"tool": tool_name, "status": "error", "reason": "Tool is not registered."})
            )
        ]

    spec = registry_cache[tool_name]
    try:
        sanitized_args = sanitize_command_arguments(spec, arguments)
        argv = render_command(spec["command"], sanitized_args)
    except Exception as exc:
        logger.exception("Failed preparing tool %s", tool_name)
        return [
            text_content(
                json.dumps(
                    {
                        "tool": tool_name,
                        "status": "preflight_error",
                        "reason": str(exc),
                    },
                    indent=2,
                )
            )
        ]

    logger.info("Executing tool %s with argv[0]=%s", tool_name, argv[0])
    try:
        completed = subprocess.run(
            argv,
            cwd=str(DATA_ROOT),
            capture_output=True,
            text=True,
            check=True,
            timeout=int(spec.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        )
        status = "ok"
        return_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.CalledProcessError as exc:
        logger.exception("Tool %s failed with exit code %s", tool_name, exc.returncode)
        status = "command_error"
        return_code = exc.returncode
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        logger.exception("Tool %s timed out", tool_name)
        status = "timeout"
        return_code = None
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
    except FileNotFoundError as exc:
        logger.exception("Executable not found for tool %s", tool_name)
        return [
            text_content(
                json.dumps(
                    {
                        "tool": tool_name,
                        "status": "executable_not_found",
                        "argv": argv,
                        "reason": str(exc),
                    },
                    indent=2,
                )
            )
        ]

    output_control = spec["output_control"]
    stdout_info = firewall_text(stdout, "stdout", output_control)
    stderr_info = firewall_text(stderr, "stderr", output_control)
    rendered_outputs, output_metadata = render_expected_outputs(spec, sanitized_args)

    notification = {
        "tool": tool_name,
        "status": status,
        "return_code": return_code,
        "argv": argv,
        "stdout": stdout_info,
        "stderr": stderr_info,
        "expected_outputs": output_metadata,
    }
    return [text_content(json.dumps(notification, indent=2)), *rendered_outputs]


def make_tool_function(spec: dict[str, Any]) -> Any:
    input_names = list(spec.get("inputs", {}).keys())
    parameters = ", ".join(f"{name}: str" for name in input_names)
    argument_dict = "{" + ", ".join(f"{name!r}: {name}" for name in input_names) + "}"
    source = (
        f"def dynamic_tool({parameters}):\n"
        f"    return _execute({spec['name']!r}, {argument_dict})\n"
    )
    namespace = {"_execute": execute_registered_tool}
    exec(source, namespace)
    function = namespace["dynamic_tool"]
    function.__name__ = spec["name"].replace("-", "_").replace(".", "_")
    function.__qualname__ = function.__name__
    function.__doc__ = spec.get("description", "")
    return function


def register_tool_spec(spec: dict[str, Any]) -> bool:
    name = spec["name"]
    registry_cache[name] = spec
    if name in registered_tool_names:
        logger.info("Tool %s already registered; cache updated only.", name)
        return False
    function = make_tool_function(spec)
    mcp.tool(name=name, description=spec.get("description", ""))(function)
    registered_tool_names.add(name)
    logger.info("Registered tool %s", name)
    return True


def load_and_register_registry() -> int:
    registry = load_effective_registry()
    specs = validate_registry(registry)
    registered_count = 0
    for spec in specs:
        if register_tool_spec(spec):
            registered_count += 1
    return registered_count


def registry_snapshot() -> dict[str, Any]:
    base_registry = load_registry_file()
    user_registry = load_user_registry_file()
    base_tools = base_registry.get("tools", [])
    user_tools = user_registry.get("tools", [])
    if not isinstance(base_tools, list):
        raise RegistryError(f"Base registry key 'tools' must be a list: {REGISTRY_PATH}")
    if not isinstance(user_tools, list):
        raise RegistryError(f"User registry key 'tools' must be a list: {USER_REGISTRY_PATH}")

    tools: list[dict[str, Any]] = []
    for source, source_path, entries in (
        ("base", REGISTRY_PATH, base_tools),
        ("user", USER_REGISTRY_PATH, user_tools),
    ):
        for entry in entries:
            if isinstance(entry, dict):
                tools.append(
                    {
                        "name": entry.get("name"),
                        "type": entry.get("type"),
                        "description": entry.get("description", ""),
                        "command": entry.get("command", ""),
                        "inputs": entry.get("inputs", {}),
                        "expected_outputs": entry.get("expected_outputs", []),
                        "source": source,
                        "source_path": str(source_path),
                    }
                )

    return {
        "base_registry_path": str(REGISTRY_PATH),
        "user_registry_path": str(USER_REGISTRY_PATH),
        "tool_bin_root": str(TOOL_BIN_ROOT),
        "tool_venv_root": str(TOOL_VENV_ROOT),
        "tool_java_root": str(TOOL_JAVA_ROOT),
        "base_tool_count": len(base_tools),
        "user_tool_count": len(user_tools),
        "effective_tool_count": len(tools),
        "runtime_registered_tool_count": len(registered_tool_names),
        "runtime_registered_tools": sorted(registered_tool_names),
        "tools": tools,
    }


def extract_single_tool_from_yaml(new_tool_yaml_str: str) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(new_tool_yaml_str)
    except yaml.YAMLError as exc:
        raise RegistryError(f"Unable to parse new tool YAML: {exc}") from exc

    if isinstance(loaded, dict) and "tools" in loaded:
        tools = loaded.get("tools")
        if not isinstance(tools, list) or len(tools) != 1:
            raise RegistryError("When using a 'tools:' block, provide exactly one tool.")
        loaded = tools[0]
    if not isinstance(loaded, dict):
        raise RegistryError("New tool YAML must be a single tool mapping.")
    return validate_tool_spec(loaded)


def safe_download_to_temp(download_url: str, destination: Path) -> None:
    parsed = urlparse(download_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("download_url must use http or https.")
    request = urllib.request.Request(download_url, headers={"User-Agent": "bio-mcp-installer/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        with destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)


def safe_extract_tar(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve(strict=False)
            if not is_relative_to(target, destination):
                raise ValueError(f"Blocked unsafe archive member path: {member.name}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    continue
                with source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)


def safe_extract_zip(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            target = (destination / member.filename).resolve(strict=False)
            if not is_relative_to(target, destination):
                raise ValueError(f"Blocked unsafe archive member path: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)


def locate_binary(search_root: Path, binary_name: str) -> Path:
    direct = search_root / binary_name
    if direct.is_file() and not direct.is_symlink():
        return direct
    for candidate in search_root.rglob(binary_name):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise FileNotFoundError(f"Unable to locate binary '{binary_name}' in downloaded artifact.")


def validate_http_url(download_url: str) -> None:
    parsed = urlparse(download_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("download_url must use http or https.")


def write_python_tool_wrapper(wrapper_path: Path, target_binary: Path) -> None:
    wrapper = f"#!/bin/sh\nexec {shlex.quote(str(target_binary))} \"$@\"\n"
    atomic_write_text(wrapper_path, wrapper)
    wrapper_path.chmod(0o755)


def locate_executable_conflict(binary_name: str, install_path: Path) -> str | None:
    existing = shutil.which(binary_name)
    if not existing:
        return None
    existing_path = Path(existing).resolve(strict=False)
    target_path = install_path.resolve(strict=False)
    if existing_path == target_path:
        return str(existing_path)
    return str(existing_path)


def reserve_executable_path(wrapper_path: Path) -> None:
    wrapper_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    placeholder = (
        "#!/bin/sh\n"
        "printf '%s\\n' 'Tool installation did not complete; remove this wrapper and retry.' >&2\n"
        "exit 126\n"
    ).encode("utf-8")
    try:
        fd = os.open(str(wrapper_path), flags, 0o755)
    except FileExistsError as exc:
        raise InstallConflictError(
            f"Tool wrapper already exists at {wrapper_path}; refusing to overwrite."
        ) from exc
    try:
        os.write(fd, placeholder)
    finally:
        os.close(fd)
    wrapper_path.chmod(0o755)


def validate_jar_name(jar_name: str) -> str:
    jar_name = jar_name.strip()
    if not jar_name:
        raise ValueError("jar_name must not be empty when provided.")
    if Path(jar_name).name != jar_name or PureWindowsPath(jar_name).name != jar_name:
        raise ValueError("jar_name must be a basename, not a path.")
    if not jar_name.lower().endswith(".jar"):
        raise ValueError("jar_name must end with .jar.")
    return jar_name


def direct_jar_filename(download_url: str) -> str | None:
    file_name = unquote(Path(urlparse(download_url).path).name)
    if not file_name.lower().endswith(".jar"):
        return None
    return validate_jar_name(file_name)


def locate_main_jar(search_root: Path, jar_name: str = "") -> Path:
    jars = sorted(
        (
            candidate
            for candidate in search_root.rglob("*")
            if candidate.is_file() and not candidate.is_symlink() and candidate.suffix.lower() == ".jar"
        ),
        key=lambda path: str(path.relative_to(search_root)),
    )
    if not jars:
        raise FileNotFoundError("No .jar files found in downloaded artifact.")
    if jar_name:
        selected_name = validate_jar_name(jar_name)
        matches = [candidate for candidate in jars if candidate.name == selected_name]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise FileNotFoundError(f"Unable to locate jar_name '{selected_name}' in downloaded artifact.")
        raise ValueError(f"Multiple .jar files named '{selected_name}' found in downloaded artifact.")
    if len(jars) == 1:
        return jars[0]
    found = ", ".join(str(path.relative_to(search_root)) for path in jars[:10])
    if len(jars) > 10:
        found = f"{found}, ..."
    raise ValueError(f"Archive contains multiple .jar files; provide jar_name. Found: {found}")


def shell_double_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def write_java_tool_wrapper(wrapper_path: Path, jar_path: Path) -> None:
    wrapper = f"#!/bin/sh\nexec java ${{JAVA_OPTS:-}} -jar {shell_double_quote(str(jar_path))} \"$@\"\n"
    atomic_write_text(wrapper_path, wrapper)
    wrapper_path.chmod(0o755)


def write_java_tool_metadata(
    metadata_path: Path,
    *,
    package_name: str,
    download_url: str,
    binary_name: str,
    jar_path: Path,
) -> None:
    metadata = {
        "package_name": package_name,
        "download_url": download_url,
        "binary_name": binary_name,
        "jar_path": str(jar_path),
    }
    atomic_write_text(metadata_path, json.dumps(metadata, indent=2, sort_keys=True) + "\n")


def install_java_url_tool(package_name: str, download_url: str, binary_name: str, jar_name: str) -> str:
    if not download_url:
        return "download_url is required for method='java_url'."
    if not SAFE_BINARY_RE.match(package_name) or package_name in {".", ".."}:
        return f"Rejected unsafe package name: {package_name!r}"
    if not SAFE_BINARY_RE.match(binary_name) or binary_name in {".", ".."}:
        return f"Rejected unsafe binary name: {binary_name!r}"
    if jar_name:
        try:
            jar_name = validate_jar_name(jar_name)
        except ValueError as exc:
            return f"Rejected unsafe jar_name: {exc}"

    package_path = TOOL_JAVA_ROOT / package_name
    wrapper_path = TOOL_BIN_ROOT / binary_name
    package_created = False
    wrapper_reserved = False
    try:
        validate_http_url(download_url)
        if package_path.exists() or package_path.is_symlink():
            raise InstallConflictError(
                f"Java package directory already exists at {package_path}; refusing to overwrite."
            )
        if wrapper_path.exists() or wrapper_path.is_symlink():
            raise InstallConflictError(
                f"Tool wrapper already exists at {wrapper_path}; refusing to overwrite."
            )
        executable_conflict = locate_executable_conflict(binary_name, wrapper_path)
        if executable_conflict:
            raise InstallConflictError(
                f"Tool wrapper name '{binary_name}' conflicts with existing executable "
                f"at {executable_conflict}; choose a unique binary_name."
            )

        TOOL_BIN_ROOT.mkdir(parents=True, exist_ok=True)
        TOOL_JAVA_ROOT.mkdir(parents=True, exist_ok=True)
        try:
            package_path.mkdir(parents=True, exist_ok=False)
            package_created = True
        except FileExistsError as exc:
            raise InstallConflictError(
                f"Java package directory already exists at {package_path}; refusing to overwrite."
            ) from exc

        try:
            reserve_executable_path(wrapper_path)
            wrapper_reserved = True
            with tempfile.TemporaryDirectory(prefix="bio-tool-install-") as tmp:
                tmp_root = Path(tmp)
                artifact = tmp_root / "downloaded_artifact"
                extracted = tmp_root / "extracted"
                safe_download_to_temp(download_url, artifact)
                jar_file_name = direct_jar_filename(download_url)
                if jar_file_name:
                    jar_path = package_path / jar_file_name
                    shutil.copy2(artifact, jar_path)
                else:
                    extracted.mkdir()
                    if tarfile.is_tarfile(artifact):
                        safe_extract_tar(artifact, extracted)
                    elif zipfile.is_zipfile(artifact):
                        safe_extract_zip(artifact, extracted)
                    else:
                        raise ValueError(
                            "download_url must point to a .jar, .zip, .tar, .tar.gz, or .tgz artifact."
                        )
                    source_jar = locate_main_jar(extracted, jar_name)
                    jar_relative_path = source_jar.relative_to(extracted)
                    shutil.copytree(extracted, package_path, dirs_exist_ok=True)
                    jar_path = package_path / jar_relative_path
            write_java_tool_wrapper(wrapper_path, jar_path)
            write_java_tool_metadata(
                package_path / "metadata.json",
                package_name=package_name,
                download_url=download_url,
                binary_name=binary_name,
                jar_path=jar_path,
            )
        except Exception:
            if wrapper_reserved and (wrapper_path.exists() or wrapper_path.is_symlink()):
                wrapper_path.unlink()
            if package_created and package_path.exists() and not package_path.is_symlink():
                shutil.rmtree(package_path)
            raise
        return (
            f"Installed Java tool '{package_name}' from URL into {package_path}. "
            f"Created wrapper '{binary_name}' at {wrapper_path}."
        )
    except InstallConflictError as exc:
        return str(exc)
    except Exception as exc:
        logger.exception("java_url install failed for %s", package_name)
        return f"java_url install failed for '{package_name}': {exc}"


@mcp.tool()
def install_bio_tool(
    method: str,
    package_name: str = "",
    download_url: str = "",
    binary_name: str = "",
    jar_name: str = "",
) -> str:
    """Install a bioinformatics tool using apt, binary/Python URLs, or a Java JAR URL."""
    method = method.strip().lower()

    if method == "apt":
        if not APT_PACKAGE_RE.match(package_name):
            return f"Rejected unsafe apt package name: {package_name!r}"
        with INSTALL_LOCK:
            try:
                subprocess.run(["apt-get", "update"], check=True, capture_output=True, text=True)
                completed = subprocess.run(
                    ["apt-get", "install", "-y", "--no-install-recommends", package_name],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return f"Installed apt package '{package_name}'.\n{completed.stdout[-4000:]}"
            except subprocess.CalledProcessError as exc:
                logger.exception("apt install failed for %s", package_name)
                return (
                    f"apt install failed for '{package_name}' with exit code {exc.returncode}.\n"
                    f"STDOUT:\n{exc.stdout[-4000:] if exc.stdout else ''}\n"
                    f"STDERR:\n{exc.stderr[-4000:] if exc.stderr else ''}"
                )

    if method == "binary_url":
        if not download_url:
            return "download_url is required for method='binary_url'."
        if not SAFE_BINARY_RE.match(binary_name):
            return f"Rejected unsafe binary name: {binary_name!r}"
        install_path = TOOL_BIN_ROOT / binary_name
        with INSTALL_LOCK:
            try:
                TOOL_BIN_ROOT.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="bio-tool-install-") as tmp:
                    tmp_root = Path(tmp)
                    artifact = tmp_root / "downloaded_artifact"
                    extracted = tmp_root / "extracted"
                    extracted.mkdir()
                    safe_download_to_temp(download_url, artifact)
                    if tarfile.is_tarfile(artifact):
                        safe_extract_tar(artifact, extracted)
                        source_binary = locate_binary(extracted, binary_name)
                    elif zipfile.is_zipfile(artifact):
                        safe_extract_zip(artifact, extracted)
                        source_binary = locate_binary(extracted, binary_name)
                    else:
                        source_binary = artifact
                    shutil.copy2(source_binary, install_path)
                    current_mode = install_path.stat().st_mode
                    install_path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                return f"Installed persistent binary '{binary_name}' at {install_path}."
            except Exception as exc:
                logger.exception("binary_url install failed for %s", binary_name)
                return f"binary_url install failed for '{binary_name}': {exc}"

    if method == "java_url":
        with INSTALL_LOCK:
            return install_java_url_tool(package_name, download_url, binary_name, jar_name)

    if method == "pip_url":
        if not download_url:
            return "download_url is required for method='pip_url'."
        if not SAFE_BINARY_RE.match(package_name):
            return f"Rejected unsafe package name: {package_name!r}"
        if not SAFE_BINARY_RE.match(binary_name):
            return f"Rejected unsafe binary name: {binary_name!r}"
        venv_path = TOOL_VENV_ROOT / package_name
        wrapper_path = TOOL_BIN_ROOT / binary_name
        with INSTALL_LOCK:
            try:
                validate_http_url(download_url)
                TOOL_BIN_ROOT.mkdir(parents=True, exist_ok=True)
                TOOL_VENV_ROOT.mkdir(parents=True, exist_ok=True)
                if venv_path.exists():
                    shutil.rmtree(venv_path)
                subprocess.run(
                    [sys.executable, "-m", "venv", str(venv_path)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                python_path = venv_path / "bin" / "python"
                binary_path = venv_path / "bin" / binary_name
                subprocess.run(
                    [str(python_path), "-m", "pip", "install", download_url],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                if not binary_path.is_file():
                    raise FileNotFoundError(
                        f"Unable to locate installed CLI '{binary_name}' at {binary_path}."
                    )
                write_python_tool_wrapper(wrapper_path, binary_path)
                return (
                    f"Installed Python package '{package_name}' from URL into {venv_path}. "
                    f"Created wrapper '{binary_name}' at {wrapper_path}."
                )
            except subprocess.CalledProcessError as exc:
                logger.exception("pip_url install failed for %s", package_name)
                return (
                    f"pip_url install failed for '{package_name}' with exit code {exc.returncode}.\n"
                    f"STDOUT:\n{exc.stdout[-4000:] if exc.stdout else ''}\n"
                    f"STDERR:\n{exc.stderr[-4000:] if exc.stderr else ''}"
                )
            except Exception as exc:
                logger.exception("pip_url install failed for %s", package_name)
                return f"pip_url install failed for '{package_name}': {exc}"

    return "Unsupported method. Use method='apt', method='binary_url', method='pip_url', or method='java_url'."


@mcp.tool()
def append_tool_to_registry(new_tool_yaml_str: str) -> str:
    """Append one validated tool YAML schema to the persistent user registry and hot-register it."""
    with REGISTRY_LOCK:
        try:
            new_tool = extract_single_tool_from_yaml(new_tool_yaml_str)
            effective_registry = load_effective_registry()
            existing_tools = effective_registry.get("tools", [])
            if not isinstance(existing_tools, list):
                raise RegistryError("Effective registry key 'tools' is not a list.")
            existing_names = {tool.get("name") for tool in existing_tools if isinstance(tool, dict)}
            if new_tool["name"] in existing_names:
                raise RegistryError(f"Tool '{new_tool['name']}' already exists in registry.")

            user_registry = load_user_registry_file()
            tools = user_registry.setdefault("tools", [])
            if not isinstance(tools, list):
                raise RegistryError("User registry key 'tools' is not a list.")

            tools.append(new_tool)
            rendered = yaml.safe_dump(user_registry, sort_keys=False, allow_unicode=False)
            atomic_write_text(USER_REGISTRY_PATH, rendered)
            load_and_register_registry()
            return (
                f"Appended and hot-registered tool '{new_tool['name']}'. "
                f"Persisted at {USER_REGISTRY_PATH}."
            )
        except Exception as exc:
            logger.exception("Failed appending tool to registry")
            return f"Failed to append tool: {exc}"


@mcp.tool()
def list_registered_tools() -> str:
    """List registered tools with names, commands, descriptions, inputs, sources, and runtime state."""
    with REGISTRY_LOCK:
        try:
            return json.dumps(registry_snapshot(), indent=2)
        except Exception as exc:
            logger.exception("Failed listing registered tools")
            return f"Failed to list registered tools: {exc}"


@mcp.tool()
def reload_registry() -> str:
    """Reload base and user registries into the running MCP server."""
    with REGISTRY_LOCK:
        try:
            newly_registered = load_and_register_registry()
            snapshot = registry_snapshot()
            return json.dumps(
                {
                    "status": "ok",
                    "newly_registered": newly_registered,
                    "effective_tool_count": snapshot["effective_tool_count"],
                    "runtime_registered_tool_count": snapshot["runtime_registered_tool_count"],
                    "runtime_registered_tools": snapshot["runtime_registered_tools"],
                },
                indent=2,
            )
        except Exception as exc:
            logger.exception("Failed reloading registry")
            return f"Failed to reload registry: {exc}"


def main() -> None:
    ensure_runtime_directories()
    try:
        count = load_and_register_registry()
        logger.info("Startup registry load complete. Newly registered tools: %s", count)
    except Exception:
        logger.exception("Startup registry load failed")
        raise

    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port_raw = os.environ.get("MCP_PORT", "8000")
    path = os.environ.get("MCP_PATH") or None
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"MCP_PORT must be an integer, got {port_raw!r}") from exc
    mcp.run(transport=transport, show_banner=False, host=host, port=port, path=path)


if __name__ == "__main__":
    main()
