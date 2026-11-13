from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ashra.api import create_app
from ashra.chat import ChatResult
from ashra.db import Database
from ashra.errors import ConfigError, IntegrationError
from ashra.integrations import (
    IntegrationConfig,
    IntegrationManager,
    RemoteTool,
    load_integrations,
    parse_integration,
)
from ashra.integrations.mcp_client import (
    SDKMCPConnection,
    _remote_tool,
    _result_payload,
    _unpack,
)
from ashra.tools import Permission, ToolRegistry


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def remote_tool(
    name: str = "search",
    description: str = "Search messages.",
    schema: dict | None = None,
) -> RemoteTool:
    return RemoteTool(
        name=name,
        description=description,
        input_schema=schema
        or {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    )


class FakeConnection:
    def __init__(
        self,
        tools: list[RemoteTool] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.tools = list(tools or [])
        self.error = error
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def list_tools(self) -> list[RemoteTool]:
        if self.error is not None:
            raise self.error
        return list(self.tools)

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, dict(arguments)))
        return {"server_tool": name, "arguments": arguments}

    def close(self) -> None:
        self.closed = True


def manager_for(
    configs: list[IntegrationConfig],
    connections: dict[str, FakeConnection | Exception],
    calls: list[str] | None = None,
) -> IntegrationManager:
    def factory(config: IntegrationConfig):
        if calls is not None:
            calls.append(config.name)
        item = connections[config.name]
        if isinstance(item, Exception):
            raise item
        return item

    return IntegrationManager(configs, connection_factory=factory)


class IntegrationConfigTests(unittest.TestCase):
    def test_parses_stdio_defaults(self) -> None:
        config = parse_integration(
            {"name": "filesystem", "command": ["python", "-m", "ashra.mcp_server"]}
        )
        self.assertEqual(config.name, "filesystem")
        self.assertEqual(config.transport, "stdio")
        self.assertEqual(config.command, ("python", "-m", "ashra.mcp_server"))
        self.assertEqual(config.permission, Permission.READ)
        self.assertTrue(config.enabled)
        self.assertEqual(config.timeout, 20.0)
        self.assertEqual(config.prefix, "filesystem")

    def test_accepts_string_command(self) -> None:
        config = parse_integration(
            {"name": "notes", "command": "python -m ashra.mcp_server --root notes"}
        )
        self.assertEqual(config.command[0], "python")
        self.assertIn("--root", config.command)

    def test_parses_http(self) -> None:
        config = parse_integration(
            {
                "name": "calendar",
                "transport": "streamable_http",
                "url": "http://127.0.0.1:8765/mcp",
                "timeout": 5,
            }
        )
        self.assertEqual(config.transport, "http")
        self.assertEqual(config.url, "http://127.0.0.1:8765/mcp")
        self.assertEqual(config.timeout, 5.0)

    def test_rejects_write_permissions(self) -> None:
        with self.assertRaises(ConfigError) as caught:
            parse_integration(
                {
                    "name": "gmail",
                    "permission": "act",
                    "command": ["python", "-m", "server"],
                }
            )
        self.assertIn("read-only", str(caught.exception))

    def test_rejects_unknown_transport(self) -> None:
        with self.assertRaises(ConfigError):
            parse_integration({"name": "x", "transport": "carrier-pigeon"})

    def test_rejects_missing_command_and_url(self) -> None:
        with self.assertRaises(ConfigError):
            parse_integration({"name": "x"})
        with self.assertRaises(ConfigError):
            parse_integration({"name": "x", "transport": "http"})

    def test_rejects_bad_name(self) -> None:
        with self.assertRaises(ConfigError):
            parse_integration({"name": "Gmail!", "command": ["python"]})

    def test_rejects_bad_timeout(self) -> None:
        for timeout in (0, 500, "soon"):
            with self.subTest(timeout=timeout), self.assertRaises(ConfigError):
                parse_integration(
                    {"name": "x", "command": ["python"], "timeout": timeout}
                )

    def test_rejects_unknown_permission(self) -> None:
        with self.assertRaises(ConfigError):
            parse_integration(
                {"name": "x", "permission": "superuser", "command": ["python"]}
            )

    def test_rejects_non_boolean_enabled(self) -> None:
        with self.assertRaises(ConfigError):
            parse_integration(
                {"name": "x", "enabled": "false", "command": ["python"]}
            )

    def test_rejects_bad_env(self) -> None:
        with self.assertRaises(ConfigError):
            parse_integration(
                {"name": "x", "command": ["python"], "env": ["A=1"]}
            )

    def test_load_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_integrations(Path(tmp) / "config.json"), [])

    def test_load_without_integrations_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"model": "test"}), encoding="utf-8")
            self.assertEqual(load_integrations(path), [])

    def test_load_parses_and_rejects_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "integrations": [
                            {"name": "gmail", "command": ["python"]},
                            {"name": "gmail", "command": ["python"]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_integrations(path)

    def test_load_rejects_colliding_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "integrations": [
                            {"name": "my-gmail", "command": ["python"]},
                            {"name": "my_gmail", "command": ["python"]},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_integrations(path)

    def test_load_rejects_non_list_integrations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"integrations": {}}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_integrations(path)


class ManagerTests(unittest.TestCase):
    def test_connects_and_registers_namespaced_tools(self) -> None:
        config = IntegrationConfig(
            name="gmail",
            transport="stdio",
            command=("python", "-m", "server"),
        )
        connection = FakeConnection([remote_tool("search")])
        manager = manager_for([config], {"gmail": connection})
        status = manager.connect_all()

        self.assertTrue(status[0]["connected"])
        self.assertEqual(status[0]["tool_count"], 0)

        registry = ToolRegistry()
        manager.register_tools(registry)
        spec = registry.get("gmail_search")
        self.assertEqual(spec.permission, Permission.READ)
        self.assertFalse(spec.requires_approval)
        self.assertIn("integration", spec.tags)
        self.assertIn("gmail", spec.tags)

        result = registry.invoke(
            "gmail_search",
            {"query": "invoice"},
            granted=Permission.READ,
        )
        self.assertEqual(result["server_tool"], "search")
        self.assertEqual(connection.calls, [("search", {"query": "invoice"})])
        self.assertEqual(manager.status()[0]["tools"], ["gmail_search"])

    def test_broken_server_does_not_block_others(self) -> None:
        gmail = IntegrationConfig(name="gmail", command=("python",))
        calendar = IntegrationConfig(name="calendar", command=("python",))
        manager = manager_for(
            [gmail, calendar],
            {
                "gmail": FakeConnection([remote_tool("search")]),
                "calendar": RuntimeError("connection refused"),
            },
        )
        status = manager.connect_all()

        self.assertTrue(status[0]["connected"])
        self.assertFalse(status[1]["connected"])
        self.assertIn("connection refused", status[1]["error"])

        registry = ToolRegistry()
        manager.register_tools(registry)
        self.assertIn("gmail_search", [spec.name for spec in registry.specs(Permission.READ)])
        self.assertNotIn("calendar_search", [spec.name for spec in registry.specs(Permission.READ)])

    def test_disabled_integration_is_skipped(self) -> None:
        config = IntegrationConfig(name="gmail", enabled=False, command=("python",))
        connection = FakeConnection([remote_tool("search")])
        manager = manager_for([config], {"gmail": connection})
        status = manager.connect_all()

        self.assertFalse(status[0]["connected"])
        self.assertFalse(status[0]["enabled"])
        self.assertEqual(connection.calls, [])

        registry = ToolRegistry()
        manager.register_tools(registry)
        self.assertEqual(registry.specs(Permission.ADMIN), [])

    def test_duplicate_local_tool_is_skipped(self) -> None:
        config = IntegrationConfig(name="gmail", command=("python",))
        manager = manager_for(
            [config],
            {"gmail": FakeConnection([remote_tool("search")])},
        )
        manager.connect_all()

        registry = ToolRegistry()
        registry.register(_local_spec("gmail_search"))
        manager.register_tools(registry)
        status = manager.status()[0]
        self.assertEqual(status["tools"], [])
        self.assertEqual(status["skipped"], ["gmail_search"])

    def test_remote_names_are_sanitized(self) -> None:
        config = IntegrationConfig(name="gmail", command=("python",))
        manager = manager_for(
            [config],
            {"gmail": FakeConnection([remote_tool("Search.Messages")])},
        )
        manager.connect_all()
        registry = ToolRegistry()
        manager.register_tools(registry)
        self.assertIn("gmail_search_messages", [spec.name for spec in registry.specs(Permission.READ)])

    def test_long_remote_names_are_truncated(self) -> None:
        config = IntegrationConfig(name="gmail", command=("python",))
        manager = manager_for(
            [config],
            {"gmail": FakeConnection([remote_tool("x" * 120)])},
        )
        manager.connect_all()
        registry = ToolRegistry()
        manager.register_tools(registry)
        names = [spec.name for spec in registry.specs(Permission.READ)]
        self.assertEqual(len(names), 1)
        self.assertLessEqual(len(names[0]), 64)

    def test_connect_all_is_idempotent(self) -> None:
        config = IntegrationConfig(name="gmail", command=("python",))
        calls: list[str] = []
        manager = manager_for(
            [config],
            {"gmail": FakeConnection([remote_tool()])},
            calls=calls,
        )
        manager.connect_all()
        manager.connect_all()
        self.assertEqual(calls, ["gmail"])

    def test_close_closes_connections(self) -> None:
        config = IntegrationConfig(name="gmail", command=("python",))
        connection = FakeConnection([remote_tool()])
        manager = manager_for([config], {"gmail": connection})
        manager.connect_all()
        manager.close()
        self.assertTrue(connection.closed)


def _local_spec(name: str):
    from ashra.tools import ToolSpec

    return ToolSpec(name=name, description="local", handler=lambda args: args)


class ClientHelperTests(unittest.TestCase):
    def test_unpack_accepts_old_and_new_shapes(self) -> None:
        read, write = object(), object()
        self.assertEqual(_unpack((read, write)), (read, write))
        self.assertEqual(_unpack((read, write, "session")), (read, write))
        streams = SimpleNamespace(read_stream=read, write_stream=write)
        self.assertEqual(_unpack(streams), (read, write))
        with self.assertRaises(IntegrationError):
            _unpack("nope")

    def test_remote_tool_accepts_both_schema_names(self) -> None:
        snake = SimpleNamespace(
            name="search",
            description="Find mail",
            input_schema={"type": "object", "properties": {}},
        )
        camel = SimpleNamespace(
            name="read",
            description=None,
            inputSchema={"type": "object", "properties": {"id": {"type": "string"}}},
        )
        self.assertEqual(_remote_tool(snake).input_schema["type"], "object")
        self.assertEqual(_remote_tool(camel).input_schema["properties"]["id"]["type"], "string")
        self.assertEqual(_remote_tool(camel).description, "")

    def test_result_payload_prefers_structured_content(self) -> None:
        result = SimpleNamespace(
            is_error=False,
            structured_content={"messages": [1, 2]},
            content=[],
        )
        self.assertEqual(_result_payload(result), {"messages": [1, 2]})

    def test_result_payload_joins_text_blocks(self) -> None:
        result = SimpleNamespace(
            is_error=False,
            structured_content=None,
            content=[SimpleNamespace(text="hello"), SimpleNamespace(text="world")],
        )
        self.assertEqual(_result_payload(result), {"text": "hello\nworld"})

    def test_result_payload_raises_on_error(self) -> None:
        result = SimpleNamespace(
            is_error=True,
            structured_content=None,
            content=[SimpleNamespace(text="permission denied")],
        )
        with self.assertRaises(IntegrationError) as caught:
            _result_payload(result)
        self.assertIn("permission denied", str(caught.exception))


@unittest.skipUnless(
    importlib.util.find_spec("mcp") is not None,
    "MCP SDK is not installed",
)
class RealSDKTests(unittest.TestCase):
    """Exercise the actual stdio client against the project's MCP server."""

    def test_lists_and_calls_a_real_stdio_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp)
            (notes / "pricing.md").write_text(
                "# Pricing\n\nOur pricing strategy is value based.\n",
                encoding="utf-8",
            )
            config = IntegrationConfig(
                name="filesystem",
                transport="stdio",
                command=(
                    sys.executable,
                    "-m",
                    "ashra.mcp_server",
                    "--root",
                    str(notes),
                ),
                cwd=str(PROJECT_ROOT),
                timeout=30,
            )
            connection = SDKMCPConnection(config)
            tools = connection.list_tools()
            self.assertIn("fs_search", [tool.name for tool in tools])

            result = connection.call_tool("fs_search", {"query": "pricing"})
            self.assertEqual(result["query"], "pricing")
            self.assertTrue(result["results"])
            self.assertEqual(result["results"][0]["path"], "pricing.md")


class FakeChat:
    def ask(self, question: str, session_id: str | None = None) -> ChatResult:
        return ChatResult(
            answer="Test answer.",
            nodes=[],
            sources=[],
            session_id="session-12345678",
            model="test-model",
        )

    def stream(self, question: str, session_id: str | None = None):
        result = self.ask(question, session_id)
        yield {"event": "start", "session_id": result.session_id, "model": result.model}
        yield {"event": "token", "text": result.answer}
        yield {
            "event": "done",
            "answer": result.answer,
            "nodes": [],
            "sources": [],
            "session_id": result.session_id,
            "model": result.model,
        }


class IntegrationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        (self.base / "viewer").mkdir()
        (self.base / "viewer" / "index.html").write_text(
            "<h1>A.S.H.R.A</h1>", encoding="utf-8"
        )
        (self.base / "notes").mkdir()
        self.db = Database(self.base / "data" / "test.db")

        config = IntegrationConfig(name="gmail", command=("python", "-m", "server"))
        self.connection = FakeConnection(
            [remote_tool("search"), remote_tool("read_message")]
        )
        self.manager = manager_for([config], {"gmail": self.connection})
        self.app = create_app(
            self.base,
            chat=FakeChat(),
            database=self.db,
            integrations=self.manager,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_integrations_endpoint_reports_tools(self) -> None:
        response = self.client.get("/integrations")
        self.assertEqual(response.status_code, 200)
        integrations = response.json()["integrations"]
        self.assertEqual(len(integrations), 1)
        self.assertEqual(integrations[0]["name"], "gmail")
        self.assertTrue(integrations[0]["connected"])
        self.assertEqual(integrations[0]["tool_count"], 2)
        self.assertIn("gmail_search", integrations[0]["tools"])

    def test_single_integration_endpoint(self) -> None:
        self.assertEqual(self.client.get("/integrations/gmail").status_code, 200)
        self.assertEqual(self.client.get("/integrations/nope").status_code, 404)

    def test_status_includes_integrations(self) -> None:
        payload = self.client.get("/status").json()
        self.assertEqual(payload["integrations"][0]["name"], "gmail")

    def test_tools_list_includes_integration_tools(self) -> None:
        payload = self.client.get("/tools?permission=read").json()
        names = [tool["name"] for tool in payload["tools"]]
        self.assertIn("gmail_search", names)
        self.assertIn("gmail_read_message", names)

    def test_tools_invoke_calls_the_remote_server(self) -> None:
        response = self.client.post(
            "/tools/invoke",
            json={
                "name": "gmail_search",
                "arguments": {"query": "invoice"},
                "permission": "read",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["server_tool"], "search")
        self.assertEqual(self.connection.calls, [("search", {"query": "invoice"})])

        events = self.client.get("/audit?event=tool&limit=10").json()["events"]
        self.assertTrue(
            any(event["action"] == "gmail_search" for event in events),
            events,
        )

    def test_shutdown_closes_the_connection(self) -> None:
        config = IntegrationConfig(name="gmail", command=("python", "-m", "server"))
        connection = FakeConnection([remote_tool("search")])
        manager = manager_for([config], {"gmail": connection})
        app = create_app(
            self.base,
            chat=FakeChat(),
            database=Database(self.base / "data" / "shutdown.db"),
            integrations=manager,
        )
        with TestClient(app):
            self.assertTrue(manager.status()[0]["connected"])
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
