#!/usr/bin/env python3
"""把仓库根的 `werewolf/` 打包成一个可直接投喂给 xun 的 extension zip。

    python pack.py                # dist/werewolf-<yyyymmdd-HHMM>.zip
    python pack.py --out DIR      # 换输出目录
    python pack.py --check X.zip  # 校验必需文件，并照 xun 的方式真加载一次入口

包里只有 `werewolf/` 这一个文件夹 —— 它就是 extension 目录本身（入口 `setup_extension.py`
在它的根部），放进 `{XUN_HOME}/extensions/` 即可（`XUN_HOME` 未设时是 `./.xun`）：

    unzip dist/werewolf-<时间戳>.zip -d "${XUN_HOME:-$PWD/.xun}/extensions/"
    # → ./.xun/extensions/werewolf/setup_extension.py（xun 的 home 在启动目录下，不在 ~）

里面只有运行时（引擎、演员、显示层、SVG 资产）；仓库根的 README.md、AGENTS.md、pack.py、
tests/ 都不进包 —— `check()` 会钉住「顶层只有 `werewolf/`」这条。

然后在 xun 会话里发 `/werewolf`。不需要 pip、不需要 PYTHONPATH。
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STAMP = datetime.now().strftime("%Y%m%d-%H%M")
#: zip 根目录名 = xun 里的 extension 名 = 仓库根那个既是引擎包又是 extension 目录的目录
PKG = "werewolf"
EXT_DIR = ROOT / PKG

SKIP_DIRS = {".git", ".dev", ".xun", "dist", "__pycache__", ".pytest_cache", ".mypy_cache"}
SKIP_SUFFIX = {".pyc", ".pyo", ".pyd", ".log", ".pid"}


#: 身份卡插画（角色表的真源在 werewolf/engine/roles.py，改角色要同时改这里和资产文件；
#: tests/test_board_cards.py 会逐个角色核对资产在位，漏了会红。）
ROLE_ART = ("wolf", "wolf_king", "seer", "witch", "hunter", "idiot", "villager")
#: 终局横幅只有 wolf / good 两张（平局与终止没有，口径见 werewolf/ui/html.py 的 BANNERS）
BANNER_ART = ("wolf", "good")

REQUIRED = (
    f"{PKG}/setup_extension.py",
    *(f"{PKG}/assets/roles/{r}.svg" for r in ROLE_ART),
    *(f"{PKG}/assets/banners/{w}.svg" for w in BANNER_ART),
    f"{PKG}/engine/engine.py",
)


def _files(base: Path):
    if not base.exists():
        raise SystemExit(f"缺少要打包的内容：{base}")
    if base.is_file():
        yield base
        return
    for path in sorted(base.rglob("*")):
        relative = path.relative_to(base)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix in SKIP_SUFFIX or not path.is_file():
            continue
        yield path


def build(out_dir: Path) -> Path:
    zip_path = out_dir / f"{PKG}-{STAMP}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in _files(EXT_DIR):          # 相对 ROOT 落位：顶层目录已经是 werewolf/
            zf.write(path, path.relative_to(ROOT))
        zf.writestr(f"{PKG}/_packaged_from.txt", (
            f"packaged_from: {EXT_DIR}\n"
            f"packaged_at:   {datetime.now().isoformat(timespec='seconds')}\n"
            f"python:        {sys.version.split()[0]}\n\n"
            "这一层就是 extension 目录本身：只有运行时，没有 README/AGENTS/pack.py/tests。\n"
            "用法：解压到 {XUN_HOME}/extensions/ 下（未设 XUN_HOME 时是 ./.xun/extensions/），\n"
            f"使最终路径成为 <extensions>/{PKG}/setup_extension.py，然后在 xun 会话里发 /werewolf\n"
        ))
    return zip_path


#: 开发期的东西不该出现在包里 —— 测试在仓库根绝对导入引擎，装好的包里没地方跑它
NOT_IN_PACKAGE = (f"{PKG}/tests/",)


def check(zip_path: Path) -> list[str]:
    """这个包不合格的问题清单：必需文件缺了，或者包外的东西混进来了。

    包里只许有 `werewolf/` 这一个顶层目录：仓库根的 README、AGENTS、pack.py、tests/ 都不该在，
    将来谁往 `build()` 里加别的来源，这里当场列出来。
    """
    if not zip_path.is_file():
        raise SystemExit(f"文件不存在：{zip_path}")
    names = set(zipfile.ZipFile(zip_path).namelist())
    problems = [f"缺必需文件：{name}" for name in REQUIRED if name not in names]
    problems += [f"包外的东西进了包：{name}" for name in sorted(names)
                 if not name.startswith(f"{PKG}/") or name.startswith(NOT_IN_PACKAGE)]
    return problems


#: 在临时目录里跑的子进程脚本：照 xun 的方式造包壳、加载入口、再懒加载引擎
_SMOKE_LOADER = """
import importlib.util, sys, types
prefix, pkg = "xun_ext_%s", "%s"
shell = types.ModuleType(prefix)
shell.__path__ = [pkg]                       # 与 xun extension.py 的做法一致
sys.modules[prefix] = shell
name = prefix + ".setup_extension"
spec = importlib.util.spec_from_file_location(name, pkg + "/setup_extension.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[name] = mod
spec.loader.exec_module(mod)                 # 加载时只该注册命令
mod._load_engine()                           # 相对导入在这一步见真章
print(mod._ENGINE.Engine.__module__)
""" % (PKG, PKG)


def load_smoke(zip_path: Path) -> tuple[bool | None, str]:
    """照 xun 的做法真加载一次：包壳 + 相对导入，只在解压后的目录结构下成立。

    必须在**子进程 + cwd=临时目录**里跑：在本进程里 `import werewolf` 会借用工作副本，
    入口哪怕写错了绝对导入也照样通过，自检就白做了。引擎是懒加载的，所以还要显式
    `_load_engine()` 才验得到相对导入那条路。返回 (是否通过, 说明)；没装 xun 时 (None, 跳过)。
    """
    import os
    import subprocess
    import tempfile

    try:
        import xun                                   # noqa: F401  入口 import 时要它
    except ImportError:
        return None, "本机没有 xun，跳过加载自检"

    with tempfile.TemporaryDirectory(prefix="werewolf-pack-check-") as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp)
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        run = subprocess.run([sys.executable, "-c", _SMOKE_LOADER], cwd=tmp, env=env,
                             capture_output=True, text=True, timeout=120)
        if run.returncode == 0:
            return True, f"入口与引擎在包壳里加载成功（{run.stdout.strip().splitlines()[-1]}）"
        return False, _root_cause(run.stderr)


def _root_cause(stderr: str) -> str:
    """从子进程 stderr 里挑出真正那句错：优先 Exception 那行，而不是我们多行提示语的尾巴。"""
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    for line in lines:
        if line.startswith("底层报错：") or line.startswith("ModuleNotFound") or line.startswith("ImportError"):
            return line[:200]
    for line in reversed(lines):
        if ": " in line and not line.startswith(("File ", "    ", "Traceback")):
            return line[:200]
    return (lines[-1] if lines else "子进程没有输出")[:200]


def report_load(zip_path: Path) -> int:
    ok, note = load_smoke(zip_path)
    if ok is None:
        print(f"⚠️  加载自检跳过：{note}")
        return 0
    print(("✅ 加载自检：" if ok else "❌ 加载自检：") + note)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pack", description="打包狼人杀为 xun extension zip")
    parser.add_argument("--out", default=str(ROOT / "dist"), help="输出目录（默认 ./dist）")
    parser.add_argument("--check", metavar="ZIP", help="校验某个 zip 的必需文件")
    args = parser.parse_args(argv)

    if args.check:
        target = Path(args.check)
        problems = check(target)
        total = len(zipfile.ZipFile(target).namelist())
        if problems:
            print(f"❌ {target.name} 不合格（{total} 个文件）：\n  " + "\n  ".join(problems))
            return 1
        print(f"✅ {target.name} 完整（{total} 个文件：必需项全在位，也没有开发期的东西）")
        return report_load(target)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = build(out_dir)
    problems = check(zip_path)
    if problems:
        zip_path.unlink(missing_ok=True)
        raise SystemExit("打包自检失败：\n  " + "\n  ".join(problems))
    print(f"✅ {zip_path}（{zip_path.stat().st_size / 1024:.0f} KB）")
    smoke_code = report_load(zip_path)
    if smoke_code:
        zip_path.unlink(missing_ok=True)
        raise SystemExit("加载自检没过，包已删除（别把装不起来的包发出去）")
    print(f"   解压到 extensions/ 即用：unzip {zip_path.name} -d <XUN_HOME>/extensions/")
    print(f"   自检：python pack.py --check {zip_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
