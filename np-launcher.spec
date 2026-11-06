# PyInstaller spec for NP Launcher (Windows)
# Build:  pip install pyinstaller && pyinstaller np-launcher.spec

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules('flask') + collect_submodules('requests')

a = Analysis(
    ['run.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('launcher/templates', 'launcher/templates'),
        ('launcher/static', 'launcher/static'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='NP Launcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,          # keep console=True so launch errors are visible; flip to False for a windowed build
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
