"""SearchOps public portfolio demo: a separate Modal app.

    modal deploy app/demo_app.py::demo   (the app object is "demo"; "app" is the package)

Isolation from the real app (app/main.py), by construction:
- Its own image with only committed, fictional inputs: no candidate_profile.yaml,
  no data/resume.docx, no real Accomplishments Inventory, no hunt_targets.yaml.
- No Volume: the database is a temp file rebuilt from demo_data/ at startup.
- No real secrets: no Gemini key (LLM calls are off in DEMO_MODE anyway), no
  Slack webhook, no notes token. The session key is random per container and
  unused (demo mode has no login).
- DEMO_MODE=1: read-only, no login, noindex (app/security/demo.py).
"""
import secrets

import modal

demo = modal.App("searchops-demo")

image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install_from_requirements("requirements.txt")
    .env({"DEMO_MODE": "1", "DATABASE_PATH": "/tmp/demo.db"})
    .add_local_python_source("app")
    .add_local_dir("app/static", remote_path="/root/app/static")
    .add_local_dir("app/templates", remote_path="/root/app/templates")
    .add_local_dir("app/voice/constraints", remote_path="/root/app/voice/constraints")
    .add_local_dir("demo_data", remote_path="/root/demo_data")
    .add_local_file("candidate_profile.example.yaml", "/root/candidate_profile.example.yaml")
    .add_local_file("data/Accomplishments_Inventory.example.docx",
                    "/root/data/Accomplishments_Inventory.example.docx")
)


@demo.function(image=image, timeout=120, max_containers=3, scaledown_window=600)
@modal.concurrent(max_inputs=32)
@modal.asgi_app()
def web():
    import os
    os.environ.setdefault("SESSION_SECRET", secrets.token_hex(32))
    os.environ.setdefault("APP_PASSWORD", secrets.token_hex(16))
    from app.demo_seed import seed_demo_db
    from app.routes import create_app
    seed_demo_db()
    return create_app()
