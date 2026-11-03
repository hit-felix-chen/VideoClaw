# -*- coding: utf-8 -*-
"""Final-cut node. The stage-machine editor is gone."""

from .base_agent import AgentInterface


class VideoEditorAgent(AgentInterface):
    """Assemble selected clips. Execution is a graph node, not a workflow stage."""

    def __init__(self):
        super().__init__(name="VideoEditor")

    def execute_node(self, call: dict) -> dict:
        return {
            "provider_called": False,
            "kind": "final_video",
            "node_id": call.get("node_id"),
        }
