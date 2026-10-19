"""Прогон задачи через реальный executor (Stage 1-7) со скриптовым LLM. Печатает трассу решений."""
import asyncio, json, logging, os, sys, tempfile
from types import SimpleNamespace

tmp = tempfile.mkdtemp(prefix="aria_trace_")
os.environ["ARIA_DATA_DIR"] = tmp
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
logging.basicConfig(level=logging.INFO, format="  LOG %(name)s: %(message)s")
for noisy in ("sqlalchemy", "httpx", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from aria.config import get_settings
from aria.db import models as m, repository as repo
from aria.db.base import init_db, session_scope
from aria.db.enums import TaskStatus
from aria.core import executor

SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "happy"
init_db(f"sqlite:///{tmp}/t.db", create_all=True)
sandbox = os.path.join(tmp, "sandbox")
get_settings().agent_sandbox_root = sandbox

PLAN = {
 "happy": [
  {"step_id": "s1", "objective": "создать character_base.py для Blender", "role": "coder", "tool_ref": "file_write",
   "path": "character_base.py", "depends_on": [], "covers": [], "write_scope": ["character_base.py"]},
  {"step_id": "s2", "objective": "проверить синтаксис скрипта", "role": "coder", "tool_ref": "shell_execute",
   "depends_on": ["s1"], "covers": [], "write_scope": []},
 ],
 "blender": [
  {"step_id": "s1", "objective": "создать character_base.py", "role": "coder", "tool_ref": "file_write",
   "path": "character_base.py", "depends_on": [], "covers": [], "write_scope": ["character_base.py"]},
  {"step_id": "s2", "objective": "запустить blender и собрать модель", "role": "devops_infra", "tool_ref": "shell_execute",
   "depends_on": ["s1"], "covers": [], "write_scope": []},
 ],
}[SCENARIO]
COMMAND = {"happy": "python -m py_compile character_base.py", "blender": "blender --background --python character_base.py"}[SCENARIO]
CONTENT = "```python\nimport bpy\nprint('human base mesh')\n```"

class ScriptedRouter:
    def __init__(self): self.calls = []
    async def route_chat(self, cls, messages, **kw):
        sysmsg = messages[0].content
        if "oracle" in sysmsg.lower() or "PlanStep" in sysmsg:
            kind, text = "ORACLE", json.dumps(PLAN)
        elif "Сгенерируй содержимое файла" in sysmsg:
            kind, text = "CONTENT", CONTENT
        elif "ОДНУ shell-команду" in sysmsg:
            kind, text = "COMMAND", COMMAND
        else:
            kind, text = "AUDIT", '{"verdict": "pass", "issues": [], "criteria": []}'
        self.calls.append(kind)
        print(f"  LLM[{cls}] -> {kind}: {text[:70]!r}")
        return SimpleNamespace(response=SimpleNamespace(text=text, tool_calls=[]), provider_id="scripted",
                               degraded_to_free=False, fallback=False)

async def main():
    router = ScriptedRouter()
    with session_scope() as db:
        s = repo.create_session(db, "trace")
        t = repo.create_task(db, s, role="general", objective="БЛЕНДЕР десктоп: определи какие скиллы нужны для моделек людей", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)
        tid = t.id
        try:
            res = await executor.run_task(db, t, router=router)
        except Exception as e:
            res = {"EXCEPTION": f"{type(e).__name__}: {e}"}
    print("\nRESULT:", res)
    with session_scope() as db:
        t = repo.get_task(db, tid)
        print("TASK STATUS:", t.status.value, "| error_code:", t.error_code, "|", (t.error_message or "")[:300])
        for c in repo.list_tool_calls(db, tid):
            print(f"  TOOL {c.tool_name:14} status={c.status.value:8} err={c.error_code} out={str(c.output_json)[:110]}")
    print("sandbox files:", [os.path.relpath(os.path.join(d, f), sandbox) for d, _, fs in os.walk(sandbox) for f in fs] if os.path.exists(sandbox) else "нет песочницы")
asyncio.run(main())
