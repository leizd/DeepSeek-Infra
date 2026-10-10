"""First-class Memory module facade for the Personal AI Runtime."""

from deepseek_infra.infra.memory.schema import MemoryRecord, public_memory
from deepseek_infra.infra.memory.search import search_memories
from deepseek_infra.infra.memory.store import add_memory, delete_memory, edit_memory, list_memories

__all__ = [
    "MemoryRecord",
    "add_memory",
    "delete_memory",
    "edit_memory",
    "list_memories",
    "public_memory",
    "search_memories",
]
