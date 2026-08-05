# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [
    ('aria/prompts', 'aria/prompts'),
    ('alembic.ini', '.'),
    ('alembic', 'alembic'),
    # Same-origin SPA: embed the built frontend so the frozen backend can
    # serve desktop/dist by itself (sys._MEIPASS/dist at runtime).
    ('../desktop/dist', 'dist'),
]
binaries = []
hiddenimports = ['aria.main', 'aria.config', 'aria.api.auth', 'aria.api.http', 'aria.api.ws', 'aria.core.loop', 'aria.core.delegate', 'aria.core.state_machine', 'aria.core.roles', 'aria.core.audit', 'aria.core.events', 'aria.db.models', 'aria.db.repository', 'aria.db.base', 'aria.llm.router', 'aria.llm.key_pool', 'aria.llm.compression', 'aria.llm.providers.base', 'aria.llm.providers.openai_compatible', 'aria.llm.providers.stub', 'aria.tools.registry', 'aria.tools.validators', 'aria.tools.handlers.files', 'aria.tools.handlers.shell', 'aria.tools.handlers.web', 'aria.storage.obsidian_vault', 'aria.storage.b2_client', 'aria.scheduler.jobs', 'aria.routers.providers', 'aria.routers.storage', 'aria.routers.sessions', 'aria.routers.tasks', 'aria.routers.system', 'aria.routers.vault', 'aria.routers.config', 'uvicorn', 'sqlalchemy', 'pydantic', 'pydantic_settings', 'alembic', 'httpx', 'httpx_sse', 'websockets', 'openai', 'dotenv', 'yaml', 'alembic.config', 'alembic.command', 'alembic.runtime.migration', 'alembic.environment', 'alembic.script']
tmp_ret = collect_all('uvicorn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('starlette')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('fastapi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['run_aria.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
