"""Build dist/in-the-know.mcpb: a one-click Claude Desktop extension (uv runtime, manifest 0.4).

    uv run python packaging/build.py
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGE = ROOT / "dist" / "bundle"
OUT = ROOT / "dist" / "in-the-know.mcpb"


def manifest() -> dict:
    import sys
    sys.path.insert(0, str(ROOT))
    import asyncio
    import tempfile
    from intheknow.context import ContextEngine
    from intheknow.mcp_server import VERSION, build_server

    srv = build_server(ContextEngine(Path(tempfile.mkdtemp()) / "m.db"))
    tools = [{"name": t.name, "description": (t.description or "").split(". ")[0][:180]} for t in asyncio.run(srv.list_tools())]
    prompts = [{"name": p.name, "description": p.description or "", "text": "(dynamic)"} for p in asyncio.run(srv.list_prompts())]
    return {
        "manifest_version": "0.4",
        "name": "in-the-know",
        "display_name": "In The Know",
        "version": VERSION,
        "description": "The memory and analyst of your small business, inside Claude.",
        "long_description": (
            "In The Know turns your messy files, photos, old chats and everything you tell Claude into one exact business "
            "memory: customers, visits or sales, prices, costs, goals and decisions. It builds a database shaped for your trade "
            "(salon, clinic, café or anything else), keeps the numbers exact, reminds you who to call, and shows beautiful cards "
            "in the chat in English, French, Romanian or German. Your data stays in a file on your computer."),
        "author": {"name": "In The Know"},
        "icon": "icon.png",
        "server": {
            "type": "uv",
            "entry_point": "intheknow/mcp_server.py",
            "mcp_config": {
                "command": "uv",
                "args": ["run", "--directory", "${__dirname}", "--frozen", "--no-dev", "python", "-m", "intheknow.mcp_server",
                         "--db", "${user_config.data_dir}/business.db"],
            },
        },
        "user_config": {
            "data_dir": {"type": "directory", "title": "Where to keep your business memory",
                         "description": "A folder on this computer. In The Know stores one database file there (business.db).",
                         "default": "${HOME}/InTheKnow", "required": True},
        },
        "tools": tools,
        "prompts": prompts,
        "prompts_generated": True,
        "keywords": ["small business", "salon", "clinic", "cafe", "crm", "memory", "analytics"],
        "license": "Proprietary",
        "compatibility": {"platforms": ["darwin", "win32", "linux"], "runtimes": {"python": ">=3.11"}},
    }


def main() -> None:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    (STAGE / "intheknow").mkdir(parents=True)
    for f in ("pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / f, STAGE / f)
    shutil.copytree(ROOT / "intheknow", STAGE / "intheknow", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(ROOT / "packaging" / "icon.png", STAGE / "icon.png")
    (STAGE / "README.md").write_text("In The Know: the memory and analyst of your small business, inside Claude.\n")
    (STAGE / "manifest.json").write_text(json.dumps(manifest(), ensure_ascii=False, indent=2))
    subprocess.run(["npx", "-y", "@anthropic-ai/mcpb@2.1.2", "validate", str(STAGE / "manifest.json")], check=True)
    subprocess.run(["npx", "-y", "@anthropic-ai/mcpb@2.1.2", "pack", str(STAGE), str(OUT)], check=True)
    print(f"built {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
