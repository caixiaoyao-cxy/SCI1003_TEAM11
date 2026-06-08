from __future__ import annotations

import functools
import json
import sys
import tarfile
import tempfile
import threading
import types
import unittest
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class DummyFastMCP:
    def __init__(self, *_args, **_kwargs):
        pass

    def tool(self, *_args, **_kwargs):
        def decorator(func):
            return func

        return decorator


class DummyContent:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


fastmcp_module = types.ModuleType("fastmcp")
fastmcp_module.FastMCP = DummyFastMCP
mcp_module = types.ModuleType("mcp")
mcp_types_module = types.ModuleType("mcp.types")
mcp_types_module.ImageContent = DummyContent
mcp_types_module.TextContent = DummyContent
sys.modules["fastmcp"] = fastmcp_module
sys.modules["mcp"] = mcp_module
sys.modules["mcp.types"] = mcp_types_module

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass


class JavaUrlInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.download_root = self.root / "downloads"
        self.download_root.mkdir()
        self.bin_root = self.root / "data" / "mcp_tools" / "bin"
        self.java_root = self.root / "data" / "mcp_tools" / "java"

        self.old_bin_root = server.TOOL_BIN_ROOT
        self.old_java_root = server.TOOL_JAVA_ROOT
        self.old_logger_disabled = server.logger.disabled
        server.TOOL_BIN_ROOT = self.bin_root
        server.TOOL_JAVA_ROOT = self.java_root
        server.logger.disabled = True

        handler = functools.partial(QuietHandler, directory=str(self.download_root))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()
        server.TOOL_BIN_ROOT = self.old_bin_root
        server.TOOL_JAVA_ROOT = self.old_java_root
        server.logger.disabled = self.old_logger_disabled
        self.tmp.cleanup()

    def url_for(self, name: str) -> str:
        return f"{self.base_url}/{name}"

    def write_zip(self, name: str, entries: dict[str, bytes]) -> None:
        with zipfile.ZipFile(self.download_root / name, "w") as archive:
            for entry_name, content in entries.items():
                archive.writestr(entry_name, content)

    def write_tar_gz(self, name: str, entries: dict[str, bytes]) -> None:
        with tarfile.open(self.download_root / name, "w:gz") as archive:
            for entry_name, content in entries.items():
                data_path = self.download_root / f"{entry_name.replace('/', '_')}.tmp"
                data_path.write_bytes(content)
                archive.add(data_path, arcname=entry_name)
                data_path.unlink()

    def test_direct_jar_install_writes_wrapper_and_metadata(self) -> None:
        (self.download_root / "picard.jar").write_bytes(b"fake jar")

        result = server.install_bio_tool(
            method="java_url",
            package_name="picard",
            download_url=self.url_for("picard.jar"),
            binary_name="picard",
        )

        jar_path = self.java_root / "picard" / "picard.jar"
        wrapper_path = self.bin_root / "picard"
        metadata_path = self.java_root / "picard" / "metadata.json"
        self.assertIn("Installed Java tool 'picard'", result)
        self.assertTrue(jar_path.is_file())
        self.assertIn("exec java ${JAVA_OPTS:-} -jar", wrapper_path.read_text(encoding="utf-8"))
        self.assertIn("picard.jar", wrapper_path.read_text(encoding="utf-8"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["package_name"], "picard")
        self.assertEqual(metadata["download_url"], self.url_for("picard.jar"))
        self.assertEqual(metadata["binary_name"], "picard")
        self.assertEqual(metadata["jar_path"], str(jar_path))

    def test_zip_with_single_jar_auto_selects_main_jar(self) -> None:
        self.write_zip("single.zip", {"release/lib/tool.jar": b"fake jar"})

        result = server.install_bio_tool(
            method="java_url",
            package_name="single_tool",
            download_url=self.url_for("single.zip"),
            binary_name="single-tool",
        )

        jar_path = self.java_root / "single_tool" / "release" / "lib" / "tool.jar"
        self.assertIn("Installed Java tool 'single_tool'", result)
        self.assertTrue(jar_path.is_file())
        metadata = json.loads((self.java_root / "single_tool" / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["jar_path"], str(jar_path))

    def test_tar_gz_with_single_jar_auto_selects_main_jar(self) -> None:
        self.write_tar_gz("single.tar.gz", {"release/lib/tool.jar": b"fake jar"})

        result = server.install_bio_tool(
            method="java_url",
            package_name="tar_tool",
            download_url=self.url_for("single.tar.gz"),
            binary_name="tar-tool",
        )

        jar_path = self.java_root / "tar_tool" / "release" / "lib" / "tool.jar"
        self.assertIn("Installed Java tool 'tar_tool'", result)
        self.assertTrue(jar_path.is_file())
        metadata = json.loads((self.java_root / "tar_tool" / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["jar_path"], str(jar_path))

    def test_zip_with_multiple_jars_requires_jar_name(self) -> None:
        self.write_zip("multi.zip", {"lib/main.jar": b"main", "lib/helper.jar": b"helper"})

        result = server.install_bio_tool(
            method="java_url",
            package_name="multi_tool",
            download_url=self.url_for("multi.zip"),
            binary_name="multi-tool",
        )

        self.assertIn("multiple .jar files", result)
        self.assertIn("jar_name", result)
        self.assertFalse((self.java_root / "multi_tool").exists())
        self.assertFalse((self.bin_root / "multi-tool").exists())

    def test_zip_with_multiple_jars_uses_explicit_jar_name(self) -> None:
        self.write_zip("multi.zip", {"lib/main.jar": b"main", "lib/helper.jar": b"helper"})

        result = server.install_bio_tool(
            method="java_url",
            package_name="multi_tool",
            download_url=self.url_for("multi.zip"),
            binary_name="multi-tool",
            jar_name="main.jar",
        )

        jar_path = self.java_root / "multi_tool" / "lib" / "main.jar"
        self.assertIn("Installed Java tool 'multi_tool'", result)
        self.assertTrue(jar_path.is_file())
        metadata = json.loads((self.java_root / "multi_tool" / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["jar_path"], str(jar_path))

    def test_existing_binary_name_refuses_overwrite(self) -> None:
        self.bin_root.mkdir(parents=True)
        (self.bin_root / "picard").write_text("existing", encoding="utf-8")

        result = server.install_bio_tool(
            method="java_url",
            package_name="picard",
            download_url=self.url_for("missing.jar"),
            binary_name="picard",
        )

        self.assertIn("Tool wrapper already exists", result)
        self.assertFalse((self.java_root / "picard").exists())

    def test_existing_package_name_refuses_overwrite(self) -> None:
        (self.java_root / "picard").mkdir(parents=True)

        result = server.install_bio_tool(
            method="java_url",
            package_name="picard",
            download_url=self.url_for("missing.jar"),
            binary_name="picard",
        )

        self.assertIn("Java package directory already exists", result)
        self.assertFalse((self.bin_root / "picard").exists())

    def test_concurrent_same_binary_name_allows_one_install_and_cleans_loser(self) -> None:
        (self.download_root / "tool.jar").write_bytes(b"fake jar")
        results: list[str] = []
        results_lock = threading.Lock()

        def install(package_name: str) -> None:
            result = server.install_bio_tool(
                method="java_url",
                package_name=package_name,
                download_url=self.url_for("tool.jar"),
                binary_name="shared-tool",
            )
            with results_lock:
                results.append(result)

        threads = [threading.Thread(target=install, args=(f"package_{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(results), 2)
        self.assertEqual(sum("Installed Java tool" in result for result in results), 1)
        self.assertEqual(sum("Tool wrapper already exists" in result for result in results), 1)
        package_dirs = [path for path in self.java_root.iterdir() if path.is_dir()]
        self.assertEqual(len(package_dirs), 1)
        self.assertTrue((self.bin_root / "shared-tool").is_file())

    def test_non_http_download_url_is_rejected(self) -> None:
        result = server.install_bio_tool(
            method="java_url",
            package_name="picard",
            download_url="file:///tmp/picard.jar",
            binary_name="picard",
        )

        self.assertIn("download_url must use http or https", result)
        self.assertFalse((self.java_root / "picard").exists())

    def test_registry_snapshot_includes_agent_discovery_fields(self) -> None:
        old_registry_path = server.REGISTRY_PATH
        old_user_registry_path = server.USER_REGISTRY_PATH
        try:
            base_registry = self.root / "registry.yaml"
            user_registry = self.root / "user_registry.yaml"
            base_registry.write_text(
                """
tools:
  - name: java_tool
    type: cli
    command: "picard --version"
    description: "Run the Picard Java wrapper."
    inputs: {}
""",
                encoding="utf-8",
            )
            user_registry.write_text("tools: []\n", encoding="utf-8")
            server.REGISTRY_PATH = base_registry
            server.USER_REGISTRY_PATH = user_registry

            snapshot = server.registry_snapshot()

            self.assertEqual(snapshot["tools"][0]["description"], "Run the Picard Java wrapper.")
            self.assertEqual(snapshot["tools"][0]["command"], "picard --version")
            self.assertEqual(snapshot["tools"][0]["inputs"], {})
        finally:
            server.REGISTRY_PATH = old_registry_path
            server.USER_REGISTRY_PATH = old_user_registry_path

    def test_concurrent_registry_appends_preserve_all_tools(self) -> None:
        old_registry_path = server.REGISTRY_PATH
        old_user_registry_path = server.USER_REGISTRY_PATH
        old_registry_cache = dict(server.registry_cache)
        old_registered_tool_names = set(server.registered_tool_names)
        try:
            base_registry = self.root / "registry.yaml"
            user_registry = self.root / "user_registry.yaml"
            base_registry.write_text("tools: []\n", encoding="utf-8")
            user_registry.write_text("tools: []\n", encoding="utf-8")
            server.REGISTRY_PATH = base_registry
            server.USER_REGISTRY_PATH = user_registry
            server.registry_cache.clear()
            server.registered_tool_names.clear()

            results: list[str] = []
            results_lock = threading.Lock()

            def append_tool(index: int) -> None:
                result = server.append_tool_to_registry(
                    f"""
name: concurrent_tool_{index}
type: cli
command: "echo concurrent_tool_{index}"
description: "Concurrent registry append test tool {index}."
inputs: {{}}
"""
                )
                with results_lock:
                    results.append(result)

            threads = [threading.Thread(target=append_tool, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)

            self.assertEqual(len(results), 2)
            self.assertTrue(all("Appended and hot-registered tool" in result for result in results))
            loaded = server.load_user_registry_file()
            names = {tool["name"] for tool in loaded["tools"]}
            self.assertEqual(names, {"concurrent_tool_0", "concurrent_tool_1"})
        finally:
            server.REGISTRY_PATH = old_registry_path
            server.USER_REGISTRY_PATH = old_user_registry_path
            server.registry_cache.clear()
            server.registry_cache.update(old_registry_cache)
            server.registered_tool_names.clear()
            server.registered_tool_names.update(old_registered_tool_names)


if __name__ == "__main__":
    unittest.main()
