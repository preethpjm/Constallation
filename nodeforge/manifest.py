"""The node contract. Every onboarded tool, whatever language or shape,
is reduced to this. Nothing else in the system knows how a tool works inside."""
from __future__ import annotations

import json
import os
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Port(BaseModel):
    name: str
    kind: Literal["file", "dir", "param"] = "file"
    mime: str = "*/*"                 # for files
    description: str = ""
    required: bool = True
    default: Optional[str] = None     # for params
    widget: str = ""                  # params: UI control hint: color | number | bool | range:0-1 | select:a|b|c | text | area
    # outputs only: where the file appears. Placeholders allowed, e.g. "{outdir}/model.glb"
    path: Optional[str] = None


class Runtime(BaseModel):
    kind: Literal["cli", "http"] = "cli"
    # cli: command template run inside the tool dir. Placeholders = port names, plus {outdir}.
    command: str = ""
    # http: start command + request description
    start: Optional[str] = None
    port: Optional[int] = None
    method: str = "POST"
    route: Optional[str] = None
    body: Literal["multipart", "json"] = "multipart"
    response_output: Optional[str] = None   # output port that receives the response body
    setup: list[str] = Field(default_factory=list)   # e.g. ["npm install"]
    base_image: str = "python:3.12-slim"             # used when packaged for docker/cloud
    timeout_s: int = 600
    env: dict[str, str] = Field(default_factory=dict)


class Verification(BaseModel):
    status: Literal["unverified", "verified", "failed"] = "unverified"
    example_inputs: dict[str, str] = Field(default_factory=dict)  # port -> path (relative to tool dir)
    example_params: dict[str, str] = Field(default_factory=dict)
    observed_outputs: dict[str, str] = Field(default_factory=dict)  # port -> sniffed mime
    log: str = ""
    attempts: int = 0


class Manifest(BaseModel):
    id: str
    name: str
    description: str = ""
    tool_dir: str                     # absolute path to the tool's source
    language: str = "unknown"
    inputs: list[Port] = Field(default_factory=list)
    outputs: list[Port] = Field(default_factory=list)
    runtime: Runtime = Field(default_factory=Runtime)
    tags: list[str] = Field(default_factory=list)
    verification: Verification = Field(default_factory=Verification)
    onboarded_by: str = "heuristic"   # heuristic | llm:<model>
    human_approved: bool = False
    origin: str = ""        # "" = a tool as found | "wrapper" = generated shim around a library function
    wrapper_of: str = ""    # wrapper only: "path/to/file.py:function"

    def file_inputs(self):
        return [p for p in self.inputs if p.kind in ("file", "dir")]

    def params(self):
        return [p for p in self.inputs if p.kind == "param"]


REGISTRY_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "registry")


def _atomic_write(path: str, text: str):
    """Readers never see a half-written file (the UI polls while onboarding writes)."""
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def save(m: Manifest, node_md: str):
    d = os.path.join(REGISTRY_DIR, m.id)
    os.makedirs(d, exist_ok=True)
    _atomic_write(os.path.join(d, "node.md"), node_md)
    _atomic_write(os.path.join(d, "manifest.json"), m.model_dump_json(indent=2))


def load_all() -> dict[str, Manifest]:
    out = {}
    if not os.path.isdir(REGISTRY_DIR):
        return out
    for nid in sorted(os.listdir(REGISTRY_DIR)):
        p = os.path.join(REGISTRY_DIR, nid, "manifest.json")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    out[nid] = Manifest.model_validate(json.load(f))
            except Exception as e:  # noqa  one broken manifest must not take the registry down
                print(f"[registry] skipping {nid}: {e}")
    return out


def load_md(nid: str) -> str:
    p = os.path.join(REGISTRY_DIR, nid, "node.md")
    try:
        return open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    except OSError:
        return ""
