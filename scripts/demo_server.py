"""Explicit synthetic demo: localhost only; never calls GEE.

Set AGRISKY_DEMO_PASSWORD before starting. This entry point is separate from
the production uvicorn command and never reads an existing production .env.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "api_gateway")]
password = os.environ.get("AGRISKY_DEMO_PASSWORD", "")
enable_model = os.environ.get("AGRISKY_DEMO_ENABLE_MODEL", "false").lower() == "true"
model_config = {name: os.environ.get(name, "") for name in
                ["AGENT_API_KEY", "AGENT_BASE_URL", "AGENT_MODEL", "AGRISKY_AGENT_API_KEY"]}
if len(password) < 12:
    raise SystemExit("Set AGRISKY_DEMO_PASSWORD to a local demo password of at least 12 characters.")
runtime = Path(os.environ.get("AGRISKY_DEMO_DIR", str(ROOT / ".runtime" / "shanghai-demo")))
runtime.mkdir(parents=True, exist_ok=True)
os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "AGRISKY_DB_PATH": str(runtime / "demo.db"),
    "AGRISKY_OUTPUT_ROOT": str(runtime / "outputs"),
    "AGRISKY_AUTH_ENABLED": "true",
    "AGRISKY_BOOTSTRAP_ADMIN_USERNAME": "reviewer",
    "AGRISKY_BOOTSTRAP_ADMIN_PASSWORD": password,
    "AGRISKY_BOOTSTRAP_ADMIN_NAME": "合成演示审核员",
    "AGRISKY_SEED_DEMO_DATA": "true",
    "AGRISKY_SEED_DEMO_ACCOUNTS": "false",
    "AGRISKY_ALLOW_MOCK_REMOTE_SENSING": "true",
    "AGRISKY_REQUIRE_CLAIM_DOCUMENTS": "false",
    "AGRISKY_COOKIE_SECURE": "false",
    "AGENT_PROVIDER": "openai_compatible",
    "AGENT_API_KEY": "",
    "AGENT_BASE_URL": "http://127.0.0.1:9",
    "AGENT_MODEL": "unconfigured-demo",
})
if enable_model:
    if not all(model_config[name] for name in ["AGENT_API_KEY", "AGENT_BASE_URL", "AGENT_MODEL"]):
        raise SystemExit("Model demo requires AGENT_API_KEY, AGENT_BASE_URL and AGENT_MODEL.")
    os.environ.update(model_config)
from space_engine import sar_flood


def synthetic_screening(**kwargs):
    return {"status": "success", "flooded_area_mu": 12.0, "damage_ratio": 0.2,
            "confidence": "mock", "reference_assets": ["synthetic:shanghai-demo"],
            "image_count": 0}


sar_flood.calculate_flood_ratio = synthetic_screening
import uvicorn

if __name__ == "__main__":
    print(f"SYNTHETIC DEMO: no real insurance case, no live GEE; live model enabled={enable_model}.")
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
