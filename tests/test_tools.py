"""Tests for agent.tools."""
import pytest
from pydantic import BaseModel
from agent.tools import Tool, ToolResult, PermissionDecision


class _DummyInput(BaseModel):
    value: str


class _DummyTool(Tool):
    name = "dummy"

    def description(self) -> str:
        return "A dummy tool for testing."

    @property
    def input_model(self):
        return _DummyInput

    def execute(self, input: _DummyInput) -> ToolResult:
        return ToolResult(output=f"got {input.value}")


def test_tool_subclass_must_implement_abstract_methods():
    """Forgetting an abstract method should fail at instantiation."""
    class Incomplete(Tool):
        name = "incomplete"
        # Missing description, input_model, execute

    with pytest.raises(TypeError):
        Incomplete()


def test_tool_default_safety_attributes_are_fail_closed():
    """Defaults: not read-only, not concurrency-safe, not destructive."""
    t = _DummyTool()
    dummy_input = _DummyInput(value="x")
    assert t.is_read_only(dummy_input) is False
    assert t.is_concurrency_safe(dummy_input) is False
    assert t.is_destructive(dummy_input) is False


def test_tool_default_permission_is_allow():
    """Base check_permissions returns ALLOW. Subclasses override for stricter."""
    t = _DummyTool()
    assert t.check_permissions(_DummyInput(value="x")) == PermissionDecision.ALLOW


def test_tool_to_anthropic_schema_shape():
    """Schema must have name, description, input_schema fields."""
    t = _DummyTool()
    schema = t.to_anthropic_schema()
    assert schema["name"] == "dummy"
    assert schema["description"] == "A dummy tool for testing."
    assert "input_schema" in schema
    # Pydantic-generated JSON schema has 'properties'
    assert "properties" in schema["input_schema"]


def test_tool_result_defaults():
    """ToolResult defaults: not error, empty metadata."""
    r = ToolResult(output="hello")
    assert r.is_error is False
    assert r.metadata == {}


def test_permission_decision_values():
    """PermissionDecision exposes ALLOW / DENY / ASK."""
    assert PermissionDecision.ALLOW == "allow"
    assert PermissionDecision.DENY == "deny"
    assert PermissionDecision.ASK == "ask"
