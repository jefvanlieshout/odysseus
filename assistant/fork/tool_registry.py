"""Compatibility shim for the v0.2.5 prototype ToolRegistry.

The canonical assistant selection adapter is now :mod:`assistant.fork.tool_catalog`.
Keep this module temporarily so local imports/tests from early v0.2.5 work do not
break while upstream Odysseus continues its own tool-registry migration.
"""
from assistant.fork.tool_catalog import (
    CAPABILITY_ANCHORS,
    DOMAIN_ANCHORS,
    ToolCatalogAudit,
    ToolRecord,
    anchor_names_for_capabilities,
    audit_tool_catalog,
    bare_tool_name,
    build_runtime_registry,
    build_tool_catalog,
    capabilities_for_name,
    connected_mcp_names,
    connected_mcp_tools,
    domain_capabilities,
    mcp_server_prefix,
    native_tool_names,
    record_for_runtime_name,
)

__all__ = [
    "CAPABILITY_ANCHORS",
    "DOMAIN_ANCHORS",
    "ToolCatalogAudit",
    "ToolRecord",
    "anchor_names_for_capabilities",
    "audit_tool_catalog",
    "bare_tool_name",
    "build_runtime_registry",
    "build_tool_catalog",
    "capabilities_for_name",
    "connected_mcp_names",
    "connected_mcp_tools",
    "domain_capabilities",
    "mcp_server_prefix",
    "native_tool_names",
    "record_for_runtime_name",
]
