#!/usr/bin/env python3
"""Smoke the v0.2.3 imagegen runtime tool against a fake image API."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
IMAGE_SMOKE = ROOT / "scripts" / "smoke-image-generation-tool-invocation.py"


def _load_image_smoke_module():
    spec = importlib.util.spec_from_file_location("ecorex_image_smoke", IMAGE_SMOKE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {IMAGE_SMOKE}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _call_tool(payload: dict[str, Any], api_base: str, output_dir: Path):
    from agent.tools.imagegen.imagegen import ImageGenTool

    saved = os.environ.copy()
    try:
        for key in (
            "GEMINI_API_KEY",
            "ARK_API_KEY",
            "DASHSCOPE_API_KEY",
            "MINIMAX_API_KEY",
            "LINKAI_API_KEY",
            "SKILL_IMAGE_GENERATION_PROVIDER",
            "SKILL_IMAGE_GENERATION_MODEL",
        ):
            os.environ.pop(key, None)
        os.environ["OPENAI_API_KEY"] = "sk-smoke-imagegen-runtime-tool"
        os.environ["OPENAI_API_BASE"] = api_base
        payload = {**payload, "output_dir": str(output_dir), "timeout": 30}
        return ImageGenTool().execute(payload)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _assert_success(label: str, result, calls: list[dict[str, Any]]) -> dict[str, Any]:
    if result.status != "success":
        raise AssertionError(f"{label} failed: {result.result}")
    payload = result.result if isinstance(result.result, dict) else {}
    images = payload.get("images") or []
    if len(images) != 1 or not Path(images[0].get("url", "")).exists():
        raise AssertionError(f"{label} output image missing: {payload}")
    route_models = [call.get("model") for call in calls]
    if route_models != ["gpt-image-2-pro", "gpt-image-2"]:
        raise AssertionError(f"{label} model fallback order mismatch: {route_models}")
    text = json.dumps(payload, ensure_ascii=False)
    if "sk-smoke-imagegen-runtime-tool" in text:
        raise AssertionError(f"{label} leaked fake API key")
    return {
        "model": payload.get("model"),
        "imageCount": len(images),
        "attemptedModels": route_models,
        "durationMs": payload.get("durationMs"),
    }


def main() -> int:
    sys.path.insert(0, str(ROOT))
    image_smoke = _load_image_smoke_module()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        output_dir = tmp_path / "outputs"
        input_image = tmp_path / "input.png"
        input_image.write_bytes(image_smoke.PNG_BYTES)
        with image_smoke.FakeImageApiServer() as api_base:
            generation = _call_tool(
                {"prompt": "runtime imagegen text-to-image smoke", "quality": "low", "output_format": "png"},
                api_base,
                output_dir,
            )
            generation_calls = list(image_smoke.FakeImageApiHandler.calls)
            image_smoke.FakeImageApiHandler.calls = []
            edit = _call_tool(
                {
                    "prompt": "runtime imagegen edit smoke",
                    "image_url": str(input_image),
                    "quality": "low",
                    "output_format": "png",
                },
                api_base,
                output_dir,
            )
            edit_calls = list(image_smoke.FakeImageApiHandler.calls)

        result = {
            "status": "PASS",
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "generation": _assert_success("generation", generation, [c for c in generation_calls if c["route"] == "generations"]),
            "edit": _assert_success("edit", edit, [c for c in edit_calls if c["route"] == "edits"]),
            "redacted": True,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
