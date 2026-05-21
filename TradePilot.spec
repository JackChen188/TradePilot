# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_data_files

spec_root = os.path.dirname(os.path.abspath(SPEC))
_extra_cfg = []
_cfg_dir = os.path.join(spec_root, 'config')
if os.path.isdir(_cfg_dir):
    for _name in os.listdir(_cfg_dir):
        _fp = os.path.join(_cfg_dir, _name)
        if os.path.isfile(_fp):
            _extra_cfg.append((_fp, 'config'))

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=collect_data_files('futu') + _extra_cfg,
    hiddenimports=[],
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
    name='TradePilot',
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
