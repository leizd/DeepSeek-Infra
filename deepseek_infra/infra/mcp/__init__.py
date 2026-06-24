"""MCP-native Tool Hub: expose the local tool runtime over the Model Context Protocol.

JSON-RPC 2.0 server (``server``), tool/resource/prompt catalogs (``registry``),
execution bridge into the policy-gated tool runtime (``adapters``), capability
scoping (``permissions``), external MCP server bridging (``bridge``, ``executor``),
and a minimal outbound Streamable-HTTP client (``client``).
"""
