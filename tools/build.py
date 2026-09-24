#!/usr/bin/env python3
"""Builds the Keypose engine runtime for the platform this runs on.

    python3 tools/build.py [--work build] [--out dist] [--ffmpeg DIR] [--mediapipe-model FILE]

Drive it with Python 3.14 or later (the licence texts come out of a .tar.zst, which tarfile reads
from 3.14). The runtime inside the pack is the pinned CPython 3.12 from runtime.json.

Every step can fail the build; nothing unverified is packed:
  1. python     fetch the pinned CPython (python-build-standalone), check its sha256, unpack
  2. packages   install exactly lock/<platform>.txt (--no-deps --require-hashes --only-binary)
  3. ffmpeg     macOS: replace OpenCV's GPL FFmpeg with the LGPL build from tools/ffmpeg.sh
  4. trim       drop what an engine never runs (pip, tkinter, headers, test suites)
  5. licences   every component's licence files -> licenses/, summary -> THIRD-PARTY-NOTICES.txt
  6. audit      every native library resolves inside the pack or to the OS; none is GPL
  7. check      precompile, run tools/smoke.py with the pack's own interpreter
  8. pack       dist/keypose-runtime-<version>-<platform>.tar.gz + .sha256 + notices + manifest entry
"""
import argparse, gzip, hashlib, json, os, platform, re, shutil, stat, struct, subprocess, sys, tarfile, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "runtime.json"), encoding="utf-8"))
VERSION = CFG["version"]
REPO_URL = os.environ.get("KEYPOSE_RUNTIME_REPO", "https://github.com/ch-h41/keypose-runtime")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def fail(msg):
    sys.exit("ERROR: " + msg)


def host_platform():
    m = platform.machine().lower()
    if sys.platform == "darwin" and m == "arm64":
        return "darwin-arm64"
    if sys.platform == "win32" and m in ("amd64", "x86_64"):
        return "win32-x64"
    fail(f"no runtime is defined for {sys.platform} {m}")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def run(cmd, **kw):
    r = subprocess.run(cmd, text=True, capture_output=True, **kw)
    if r.returncode:
        fail(f"{' '.join(map(str, cmd))} exited {r.returncode}\n{r.stdout[-4000:]}\n{r.stderr[-4000:]}")
    return r.stdout


def fetch(url, dest, want):
    """Download once into the cache; a cached file is re-verified, never trusted."""
    if os.path.exists(dest) and sha256(dest) == want:
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    log("download", url)
    part = dest + ".part"
    with urllib.request.urlopen(url, timeout=120) as r, open(part, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    got = sha256(part)
    if got != want:
        os.remove(part)
        fail(f"{url}: sha256 {got}, runtime.json pins {want}")
    os.replace(part, dest)
    return dest


class Layout:
    """Where things are inside a python-build-standalone install on each OS."""
    def __init__(self, rt, plat):
        self.rt, self.plat, self.win = rt, plat, plat.startswith("win32")
        py = CFG["python"]["version"].rsplit(".", 1)[0]
        self.python = os.path.join(rt, "python.exe") if self.win else os.path.join(rt, "bin", "python3")
        self.stdlib = os.path.join(rt, "Lib") if self.win else os.path.join(rt, "lib", "python" + py)
        self.site = os.path.join(self.stdlib, "site-packages")


# ---------------------------------------------------------------- 1. python
def unpack_python(plat, cache, work):
    spec = CFG["python"][plat]["install"]
    archive = fetch(CFG["python"]["base_url"] + spec["file"].replace("+", "%2B"), os.path.join(cache, spec["file"]), spec["sha256"])
    stage = os.path.join(work, "stage")
    shutil.rmtree(stage, ignore_errors=True)
    with tarfile.open(archive, "r:gz") as t:
        t.extractall(stage, filter="data")
    rt = os.path.join(work, "keypose-runtime")
    shutil.rmtree(rt, ignore_errors=True)
    os.replace(os.path.join(stage, "python"), rt)
    shutil.rmtree(stage)
    return rt


# ---------------------------------------------------------------- 2. packages
def install_packages(L, cache):
    lock = os.path.join(ROOT, "lock", L.plat + ".txt")
    env = dict(os.environ, PIP_CACHE_DIR=os.path.join(cache, "pip"), PIP_DISABLE_PIP_VERSION_CHECK="1")
    run([L.python, "-E", "-s", "-m", "pip", "install", "--no-deps", "--only-binary=:all:", "--require-hashes",
         "--no-compile", "--no-warn-script-location", "--progress-bar", "off", "-r", lock], env=env)
    # --no-deps means nothing checked the declared dependencies; pip check does, and the only gaps
    # allowed are the ones runtime.json names on purpose
    r = subprocess.run([L.python, "-E", "-s", "-m", "pip", "check"], text=True, capture_output=True, env=env)
    allowed = {(k.lower(), d.lower()) for k, v in CFG.get("omitted_dependencies", {}).items() for d in v}
    bad = []
    for line in r.stdout.splitlines():
        m = re.match(r"(\S+) \S+ requires (\S+?),", line)
        if not (m and (m.group(1).lower(), m.group(2).lower()) in allowed):
            bad.append(line)
    if bad:
        fail("dependency check:\n  " + "\n  ".join(bad))


def installed(L):
    """[(name, version, dist-info path)] from site-packages."""
    out = []
    for d in sorted(os.listdir(L.site)):
        if d.endswith(".dist-info"):
            meta = parse_metadata(os.path.join(L.site, d, "METADATA"))
            out.append((meta.get("Name", [d])[0], meta.get("Version", ["?"])[0], os.path.join(L.site, d), meta))
    return out


def parse_metadata(path):
    meta = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.strip():
                break                                   # headers end at the first blank line
            if line[0] in " \t":
                continue
            k, _, v = line.partition(":")
            meta.setdefault(k.strip(), []).append(v.strip())
    return meta


# ---------------------------------------------------------------- 3. ffmpeg (macOS)
FFMPEG_LIBS = ("avcodec", "avformat", "avutil", "swscale", "avdevice", "swresample", "avfilter", "postproc")


def macho_commands(path):
    """[(load command, path)] for the arm64 slice. `otool -L` is not used: it prints the library's own
    install name as if it were a dependency, and a header per slice of a universal binary."""
    out, cmd = [], None
    for line in run(["otool", "-arch", "arm64", "-l", path]).splitlines():
        s = line.strip()
        if s.startswith("cmd "):
            cmd = s.split()[1]
        elif s.startswith(("name ", "path ")) and cmd:
            out.append((cmd, s[5:].rsplit(" (offset", 1)[0]))
    return out


def macho_deps(path, weak=False):
    kinds = ("LC_LOAD_DYLIB", "LC_REEXPORT_DYLIB", "LC_LAZY_LOAD_DYLIB") + (("LC_LOAD_WEAK_DYLIB",) if weak else ())
    return [p for c, p in macho_commands(path) if c in kinds]


def is_macho(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca")
    except OSError:
        return False


def swap_ffmpeg(L, ffmpeg_dir):
    """Point cv2 at the LGPL FFmpeg and drop every library only the GPL one needed."""
    dylibs = os.path.join(L.site, "cv2", ".dylibs")
    cv2so = os.path.join(L.site, "cv2", "cv2.abi3.so")
    ours = {}
    for name in FFMPEG_LIBS:
        link = [f for f in os.listdir(os.path.join(ffmpeg_dir, "lib")) if re.fullmatch(rf"lib{name}\.\d+\.dylib", f)]
        if link:
            ours[name] = os.path.join(ffmpeg_dir, "lib", link[0])     # lib<name>.<major>.dylib, its install name
    changed = []
    for dep in macho_deps(cv2so):
        base = os.path.basename(dep)
        name = base[3:].split(".")[0] if base.startswith("lib") else None
        if name in FFMPEG_LIBS:
            if name not in ours:
                fail(f"cv2 links {base} but the LGPL FFmpeg build has no lib{name}")
            new = "@loader_path/.dylibs/" + os.path.basename(ours[name])
            run(["install_name_tool", "-change", dep, new, cv2so])
            changed.append((base, os.path.basename(ours[name])))
    if not changed:
        fail("cv2 does not link FFmpeg the way this script expects - check the OpenCV wheel")
    for name, src in ours.items():
        shutil.copy2(os.path.realpath(src), os.path.join(dylibs, os.path.basename(src)))
    run(["codesign", "--force", "--sign", "-", cv2so])    # install_name_tool voids the signature; arm64 refuses unsigned code
    # everything under .dylibs that cv2 no longer reaches goes
    keep, todo = set(), [cv2so]
    while todo:
        for dep in macho_deps(todo.pop()):
            if dep.startswith("@loader_path/"):
                n = os.path.basename(dep)
                p = os.path.join(dylibs, n)
                if n not in keep and os.path.exists(p):
                    keep.add(n)
                    todo.append(p)
    removed = sorted(n for n in os.listdir(dylibs) if n not in keep)
    for n in removed:
        os.remove(os.path.join(dylibs, n))
    dist = [d for d in os.listdir(L.site) if d.startswith("opencv_python_headless-") and d.endswith(".dist-info")][0]
    with open(os.path.join(L.site, dist, "KEYPOSE-CHANGES.txt"), "w", encoding="utf-8") as f:
        f.write("keypose-runtime changed this package after installing it (tools/build.py swap_ffmpeg):\n\n"
                "cv2/cv2.abi3.so now loads FFmpeg from an LGPL-2.1-or-later build made by tools/ffmpeg.sh, in place of the\n"
                "GPL-3.0-or-later Homebrew FFmpeg the wheel bundled. Its load commands were rewritten with install_name_tool\n"
                "and it was re-signed ad hoc; OpenCV's own code is unchanged.\n\nRelinked:\n"
                + "".join(f"  {a} -> {b}\n" for a, b in changed)
                + f"\nRemoved from cv2/.dylibs ({len(removed)} libraries no longer referenced):\n"
                + "".join(f"  {n}\n" for n in removed))
    log(f"ffmpeg: relinked {len(changed)}, kept {len(keep)} libraries, removed {len(removed)}")
    return {"relinked": changed, "removed": removed}


# ---------------------------------------------------------------- 4. trim
# Python extension modules trim() deletes: the notices leave them (and licences only they brought) out
TRIMMED_EXTENSIONS = {"_tkinter"}


def trim(L):
    """Only what the engine never runs. Package contents are left whole apart from test suites."""
    rt, gone = L.rt, []
    def rm(*parts):
        p = os.path.join(rt, *parts)
        if os.path.islink(p) or os.path.isfile(p):
            gone.append(os.path.getsize(p) if os.path.isfile(p) else 0); os.remove(p)
        elif os.path.isdir(p):
            gone.append(tree_size(p)); shutil.rmtree(p)
    site = os.path.relpath(L.site, rt)
    std = os.path.relpath(L.stdlib, rt)
    for d in os.listdir(L.site):                        # pip and friends: a pack is never installed into
        if re.match(r"(pip|setuptools|wheel|_distutils_hack)([-_.].*)?$", d) or d == "distutils-precedence.pth":
            rm(site, d)
    for d in ("idlelib", "tkinter", "turtledemo", "ensurepip", "test"):
        rm(std, d)
    rm("include"); rm("share")
    if L.win:
        for d in ("Scripts", "libs", "tcl"):
            rm(d)
        for f in os.listdir(os.path.join(rt, "DLLs")):
            if re.match(r"(_tkinter\.pyd|tcl\d.*\.dll|tk\d.*\.dll)$", f, re.I):
                rm("DLLs", f)
    else:
        for f in os.listdir(os.path.join(rt, "bin")):
            if not re.fullmatch(r"python3(\.\d+)?", f):
                rm("bin", f)
        for f in os.listdir(os.path.join(rt, "lib")):
            if re.match(r"(tcl|tk|itcl|thread|libtcl|libtk|pkgconfig)", f):
                rm("lib", f)
        dyn = os.path.join(L.stdlib, "lib-dynload")
        for f in os.listdir(dyn):
            if f.startswith("_tkinter"):
                rm(os.path.relpath(dyn, rt), f)
    for top, dirs, _ in os.walk(L.site):                # test suites shipped inside wheels
        for d in list(dirs):
            if d in ("tests", "test") and os.path.relpath(top, L.site).split(os.sep)[0] in ("numpy", "mediapipe"):
                rm(os.path.relpath(os.path.join(top, d), rt)); dirs.remove(d)
    log(f"trim: removed {sum(gone) / 1e6:.1f} MB")


def tree_size(p):
    return sum(os.path.getsize(os.path.join(a, f)) for a, _, fs in os.walk(p) for f in fs if not os.path.islink(os.path.join(a, f)))


# ---------------------------------------------------------------- 5. licences
LICENCE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|ThirdPartyNotices|AUTHORS)[^/]*$", re.I)


def licence_of(meta):
    if meta.get("License-Expression"):
        return meta["License-Expression"][0]
    lic = (meta.get("License") or [""])[0].strip()[:80]
    cls = [c.split("::")[-1].strip() for c in meta.get("Classifier", []) if c.startswith("License ::")]
    return lic or ", ".join(cls) or "see its licence files"


def collect_licences(L, plat, cache, ffmpeg_dir, vc_runtime=()):
    out = os.path.join(L.rt, "licenses")
    shutil.rmtree(out, ignore_errors=True)
    rows = []
    # CPython and what python-build-standalone compiled into it: the licence texts ship only in the
    # "full" archive of the same build, so stream just those members out of it (pinned sha256)
    spec = CFG["python"][plat]["full"]
    full = fetch(CFG["python"]["base_url"] + spec["file"].replace("+", "%2B"), os.path.join(cache, spec["file"]), spec["sha256"])
    pdir = os.path.join(out, "python")
    os.makedirs(pdir)
    ext_lic = {}
    with tarfile.open(full, "r:zst") as t:
        for m in t:
            if m.isfile() and m.name.startswith("python/licenses/"):
                with t.extractfile(m) as src, open(os.path.join(pdir, os.path.basename(m.name)), "wb") as dst:
                    shutil.copyfileobj(src, dst)
            elif m.name == "python/PYTHON.json":
                pj = json.load(t.extractfile(m))
                for ext, variants in pj["build_info"]["extensions"].items():
                    if ext in TRIMMED_EXTENSIONS:
                        continue
                    for v in variants:
                        for lic in v.get("licenses") or []:
                            ext_lic.setdefault(lic, set()).add(ext)
    rows.append(("Python (CPython)", CFG["python"]["version"], "PSF-2.0", "licenses/python/LICENSE.cpython.txt",
                 "built by python-build-standalone; bundled libraries below"))
    for lic, exts in sorted(ext_lic.items()):
        rows.append(("  in Python: " + ", ".join(sorted(exts)), "", lic, "licenses/python/", ""))
    # every installed package: PEP 639 licence folders, top-level licence files, and notices kept
    # inside the package itself (onnxruntime/ThirdPartyNotices.txt, cv2/LICENSE-3RD-PARTY.txt)
    for name, version, dist, meta in installed(L):
        dest = os.path.join(out, name.lower())
        files = []
        record = os.path.join(dist, "RECORD")
        for line in open(record, encoding="utf-8"):
            rel = line.split(",")[0]
            if LICENCE_FILE.search(rel) or "/licenses/" in rel and rel.split("/")[0].endswith(".dist-info"):
                src = os.path.join(L.site, rel)
                if os.path.isfile(src):
                    parts = rel.split("/")
                    tail = "/".join(parts[2:]) if parts[0].endswith(".dist-info") and len(parts) > 2 and parts[1] == "licenses" else \
                           "/".join(parts[1:]) if parts[0].endswith(".dist-info") else "package/" + rel
                    os.makedirs(os.path.dirname(os.path.join(dest, tail)), exist_ok=True)
                    shutil.copyfile(src, os.path.join(dest, tail))
                    files.append(tail)
        extra = os.path.join(ROOT, "extra-licenses", name.lower())
        if not files and os.path.isdir(extra):       # a wheel that ships no licence text: the upstream file, kept here
            shutil.copytree(extra, dest, dirs_exist_ok=True)
            files = os.listdir(extra)
        if not files:
            fail(f"{name} {version} ships no licence file - add its upstream licence to extra-licenses/{name.lower()}/")
        changes = os.path.join(dist, "KEYPOSE-CHANGES.txt")
        if os.path.exists(changes):
            shutil.copyfile(changes, os.path.join(dest, "KEYPOSE-CHANGES.txt"))
        rows.append((name, version, licence_of(meta), f"licenses/{name.lower()}/", ""))
    if vc_runtime:
        # app-local copies of the Visual C++ runtime, which Microsoft allows redistributing with an
        # application; numpy's wheel carries one the same way
        vdir = os.path.join(out, "msvc-runtime")
        os.makedirs(vdir)
        with open(os.path.join(vdir, "NOTICE.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write("Microsoft Visual C++ runtime files, next to python.exe: " + ", ".join(vc_runtime) + "\n\n"
                    "Copyright (c) Microsoft Corporation. Redistributed unmodified, as app-local copies, under the\n"
                    "Visual Studio distributable-code terms for the Visual C++ runtime:\n"
                    "https://learn.microsoft.com/visualstudio/releases/2022/redistribution#visual-c-runtime-files\n"
                    "Bundled because onnxruntime and mediapipe need them and a Windows machine is not guaranteed\n"
                    "to have the Visual C++ Redistributable installed.\n")
        rows.append(("Microsoft Visual C++ runtime", "", "Microsoft distributable code", "licenses/msvc-runtime/",
                     ", ".join(vc_runtime)))
    if ffmpeg_dir:
        fdir = os.path.join(out, "ffmpeg")
        shutil.copytree(os.path.join(ffmpeg_dir, "licenses"), fdir)
        shutil.copyfile(os.path.join(ffmpeg_dir, "BUILD.txt"), os.path.join(fdir, "BUILD.txt"))
        rows.append(("FFmpeg (libraries used by OpenCV)", CFG["ffmpeg"]["version"], "LGPL-2.1-or-later", "licenses/ffmpeg/",
                     "built by this project, see licenses/ffmpeg/BUILD.txt"))
    write_notices(L, plat, rows, bool(ffmpeg_dir))
    return rows


def write_notices(L, plat, rows, swapped):
    w = max(len(r[0]) for r in rows)
    lines = [
        f"Keypose engine runtime {VERSION} ({plat}) - third-party notices",
        "=" * 72, "",
        "This folder is the Python runtime the Keypose tracking engine runs on. It contains only the",
        "open-source software listed below, each under its own licence, unmodified except where noted.",
        "The full licence texts are in the licenses/ folder next to this file.", "",
        f"How it was built, and where to get the source of every component: {REPO_URL}",
        f"(SOURCES.md there; this release: {REPO_URL}/releases/tag/v{VERSION})", "",
        f"{'Component'.ljust(w)}  {'Version'.ljust(10)}  Licence",
        f"{'-' * w}  {'-' * 10}  {'-' * 30}",
    ]
    for name, ver, lic, where, note in rows:
        lines.append(f"{name.ljust(w)}  {ver.ljust(10)}  {lic}")
        if note:
            lines.append(f"{''.ljust(w)}  {''.ljust(10)}  ({note})")
    lines += ["", "Notes", "-----",
              "* Dependencies declared by mediapipe but left out on purpose, because Keypose does not use them:",
              "  " + ", ".join(CFG.get("omitted_dependencies", {}).get("mediapipe", [])) + ".",
              "* Removed to save space: pip, Python's tkinter/IDLE, and the test suites inside numpy and mediapipe."]
    if swapped:
        lines += ["* opencv-python-headless: on macOS the wheel bundles a GPL-licensed FFmpeg. This runtime",
                  "  replaces it with an LGPL-2.1-or-later FFmpeg built from the official source (see",
                  "  licenses/ffmpeg/BUILD.txt), relinks cv2 to it, and leaves out the libraries only the",
                  "  replaced FFmpeg needed. Details: licenses/opencv-python-headless/KEYPOSE-CHANGES.txt.",
                  "  You may replace these FFmpeg libraries (cv2/.dylibs/libav*.dylib, libsw*.dylib) with",
                  "  your own build of the same major versions."]
    else:
        lines += ["* opencv-python-headless: OpenCV loads FFmpeg (LGPL-2.1-or-later) from",
                  "  cv2/opencv_videoio_ffmpeg*.dll, a separate library you may replace with your own build."]
    lines += ["* LGPL and MPL components (FFmpeg; certifi's CA bundle): see SOURCES.md in the repository",
              "  above for their corresponding source.", ""]
    with open(os.path.join(L.rt, "THIRD-PARTY-NOTICES.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------- 6. audit
# FFmpeg compiles exactly one licence string in (avutil_license) and its configure line; a GPL build says
# "GPL version 2/3 or later" and was configured with --enable-gpl. (Not "x264 - core": the H.264
# DECODER contains that text to recognise x264-encoded files, so every FFmpeg has it.)
GPL_MARKERS = [re.compile(rb"(?<!L)GPL version [23]"), re.compile(rb"--enable-gpl")]
GPL_NAMES = re.compile(r"(libx264|libx265|libpostproc|librubberband|libvidstab|libxvidcore|frei0r)", re.I)
VC_RUNTIME = re.compile(r"^(msvcp140(_\d+|_atomic_wait|_codecvt_ids)?|vcruntime140(_\d+)?|concrt140|vcomp140)\.dll$", re.I)


def native_files(L):
    for top, _, files in os.walk(L.rt):
        for f in files:
            p = os.path.join(top, f)
            if os.path.islink(p):
                continue
            if L.win and f.lower().endswith((".dll", ".pyd", ".exe")) or not L.win and (f.endswith((".so", ".dylib")) or is_macho(p)):
                yield p


def pe_imports(path):
    """DLL names a PE file imports (normal and delay-load), from its import directories."""
    with open(path, "rb") as f:
        d = f.read()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec, optsize = struct.unpack_from("<H", d, pe + 6)[0], struct.unpack_from("<H", d, pe + 20)[0]
    opt = pe + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    dirs = opt + (112 if magic == 0x20B else 96)
    secs = [struct.unpack_from("<8sIIII", d, opt + optsize + 40 * i) for i in range(nsec)]
    def off(rva):
        for _, vsize, va, rsize, raw in secs:
            if va <= rva < va + max(vsize, rsize):
                return rva - va + raw
        return None
    def cstr(rva):
        o = off(rva)
        return d[o:d.index(b"\0", o)].decode("ascii", "replace") if o is not None else None
    out = {"import": [], "delay": []}
    for kind, idx, size, name_at in (("import", 1, 20, 12), ("delay", 13, 32, 4)):
        rva = struct.unpack_from("<I", d, dirs + 8 * idx)[0]
        o = off(rva) if rva else None
        while o is not None and o + size <= len(d):
            ent = d[o:o + size]
            if not any(ent):
                break
            n = cstr(struct.unpack_from("<I", ent, name_at)[0])
            if n:
                out[kind].append(n)
            o += size
    return out


def audit(L):
    problems, notes, vc_needed = [], [], set()
    files = list(native_files(L))
    for p in files:
        rel = os.path.relpath(p, L.rt)
        if GPL_NAMES.search(os.path.basename(p)):
            problems.append(f"{rel}: a GPL library by name")
        with open(p, "rb") as f:
            blob = f.read()
        for m in GPL_MARKERS:
            hit = m.search(blob)
            if hit:
                problems.append(f"{rel}: contains {hit.group(0)[:40]!r} (GPL-licensed code)")
                break
        if L.win:
            imp = pe_imports(p)
            here = [os.path.dirname(p), L.rt, os.path.join(L.rt, "DLLs")]
            libs = [os.path.join(L.site, d) for d in os.listdir(L.site) if d.endswith(".libs")]   # delvewheel folders
            for dll, optional in [(n, False) for n in imp["import"]] + [(n, True) for n in imp["delay"]]:
                if any(os.path.exists(os.path.join(h, dll)) for h in here + libs):
                    continue
                if VC_RUNTIME.match(dll):
                    vc_needed.add(dll.lower()); continue
                if dll.lower().startswith(("api-ms-win-", "ext-ms-")) or os.path.exists(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", dll)):
                    continue
                (notes if optional else problems).append(f"{rel}: {'delay-loads' if optional else 'imports'} {dll}, found nowhere")
        else:
            cmds = macho_commands(p)
            here, exe = os.path.dirname(p), os.path.join(L.rt, "bin")
            expand = lambda s: s.replace("@loader_path", here).replace("@executable_path", exe)
            rpaths = [expand(v) for c, v in cmds if c == "LC_RPATH"] + [here]
            for c, dep in cmds:
                if c not in ("LC_LOAD_DYLIB", "LC_REEXPORT_DYLIB", "LC_LAZY_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB"):
                    continue
                if dep.startswith(("/usr/lib/", "/System/Library/")):
                    continue
                if dep.startswith("@rpath/"):
                    found = any(os.path.exists(os.path.join(r, dep[len("@rpath/"):])) for r in rpaths)
                elif dep.startswith(("@loader_path/", "@executable_path/")):
                    found = os.path.exists(expand(dep))
                else:
                    found = False
                if not found:
                    (notes if c == "LC_LOAD_WEAK_DYLIB" else problems).append(f"{rel}: loads {dep}, which is not in the runtime")
    # the MSVC runtime: bundle it next to python.exe if anything needs it, so a machine without the
    # Visual C++ redistributable still runs the engine (python.exe's folder is on the DLL search path)
    bundled = []
    for dll in sorted(vc_needed):
        src = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", dll)
        if not os.path.exists(src):
            problems.append(f"{dll} is needed and not on this build machine to bundle")
            continue
        shutil.copy2(src, os.path.join(L.rt, dll))
        bundled.append(dll)
    if L.win:
        longest = max((os.path.relpath(os.path.join(a, f), L.rt) for a, _, fs in os.walk(L.rt) for f in fs), key=len)
        # %APPDATA%\Afterimage\engine\.runtime.new\ is ~75 characters; Windows paths stop at 259
        if len(longest) > 175:
            problems.append(f"path too long for Windows once installed ({len(longest)}): {longest}")
    for n in notes:
        log("audit note:", n)
    if problems:
        fail("audit:\n  " + "\n  ".join(problems))
    log(f"audit: {len(files)} native files clean" + (f"; bundled {', '.join(bundled)}" if bundled else ""))
    return {"native_files": len(files), "vc_runtime_bundled": bundled}


# ---------------------------------------------------------------- 7. check
def precompile(L):
    # unchecked-hash: the pack is never edited after this, so the bytecode never needs rechecking and
    # the engine never writes .pyc files into the runtime folder
    r = subprocess.run([L.python, "-E", "-s", "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash", L.site],
                       text=True, capture_output=True)
    if r.returncode:
        log("compileall reported (files that are not Python 3.12 source are skipped):\n" + (r.stdout + r.stderr)[-2000:])


def smoke(L, work, model):
    out = os.path.join(work, "smoke.json")
    cmd = [L.python, "-E", "-s", os.path.join(ROOT, "tools", "smoke.py"), out] + ([os.path.abspath(model)] if model else [])   # it runs from the work folder
    r = subprocess.run(cmd, text=True, capture_output=True, cwd=work)
    print(r.stdout[-6000:], r.stderr[-3000:])
    if r.returncode:
        fail("smoke test failed")
    return json.load(open(out, encoding="utf-8"))


# ---------------------------------------------------------------- 8. pack
def manifest(L, plat, rows, ffmpeg, audit_info, smoke_info):
    m = {
        "name": CFG["name"], "version": VERSION, "platform": plat, "min_os": CFG["platforms"][plat]["min_os"],
        "python": CFG["python"]["version"],
        "packages": {n: v for n, v, _, _ in installed(L)},
        "ffmpeg_replaced": ffmpeg, "audit": audit_info,
        "smoke": {k: v.get("info") for k, v in smoke_info["checks"].items()},
        "source": REPO_URL, "commit": os.environ.get("GITHUB_SHA", "local"),
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(os.path.join(L.rt, "keypose-runtime.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(m, f, indent=1)
    return m


def pack(L, plat, out):
    os.makedirs(out, exist_ok=True)
    name = f"keypose-runtime-{VERSION}-{plat}.tar.gz"
    path = os.path.join(out, name)
    entries = [L.rt]                                         # sorted, so the archive does not depend on directory order
    for top, dirs, files in os.walk(L.rt):
        dirs.sort()
        entries += [os.path.join(top, n) for n in dirs + sorted(files)]
    size = 0
    with open(path + ".part", "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as t:
        for entry in entries:
            rel = os.path.relpath(entry, L.rt).replace(os.sep, "/")
            ti = t.gettarinfo(entry, arcname="keypose-runtime" if rel == "." else "keypose-runtime/" + rel)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if L.win:                                        # Windows has no mode bits worth keeping
                ti.mode = 0o755 if ti.isdir() or entry.lower().endswith((".exe", ".dll", ".pyd")) else 0o644
            if ti.isfile():
                size += ti.size
                with open(entry, "rb") as src:
                    t.addfile(ti, src)
            else:
                t.addfile(ti)
    os.replace(path + ".part", path)
    digest = sha256(path)
    with open(path + ".sha256", "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{digest}  {name}\n")
    shutil.copyfile(os.path.join(L.rt, "THIRD-PARTY-NOTICES.txt"), os.path.join(out, f"keypose-runtime-{VERSION}-{plat}-THIRD-PARTY-NOTICES.txt"))
    entry = {"file": name, "sha256": digest, "size": os.path.getsize(path), "unpacked": size,
             "min_os": CFG["platforms"][plat]["min_os"]}
    with open(os.path.join(out, f"{plat}.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(entry, f, indent=1)
    log(f"pack: {name} {entry['size'] / 1e6:.1f} MB ({size / 1e6:.0f} MB unpacked) sha256 {digest}")
    return entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=os.path.join(ROOT, "build"))
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--ffmpeg", help="macOS: an existing tools/ffmpeg.sh output folder (else it is built)")
    ap.add_argument("--mediapipe-model", help="a holistic_landmarker.task for the smoke test to run")
    a = ap.parse_args()
    if sys.version_info < (3, 14):
        fail("drive the build with Python 3.14+ (tarfile needs zstd for the licence archive)")
    plat = host_platform()
    work = os.path.join(os.path.abspath(a.work), plat)
    cache = os.path.join(os.path.abspath(a.work), "cache")
    os.makedirs(work, exist_ok=True)
    log(f"keypose-runtime {VERSION} for {plat}")

    L = Layout(unpack_python(plat, cache, work), plat)
    log("python:", run([L.python, "-E", "-s", "-c", "import sys; print(sys.version)"]).strip())
    install_packages(L, cache)
    log("packages:", ", ".join(f"{n} {v}" for n, v, _, _ in installed(L)))

    ffmpeg_dir, swapped = None, None
    if plat in CFG["ffmpeg"]["platforms"]:
        ffmpeg_dir = os.path.abspath(a.ffmpeg) if a.ffmpeg else os.path.join(cache, f"ffmpeg-{CFG['ffmpeg']['version']}-{plat}")
        if not os.path.exists(os.path.join(ffmpeg_dir, "BUILD.txt")):
            subprocess.run(["bash", os.path.join(ROOT, "tools", "ffmpeg.sh"), ffmpeg_dir], check=True)
        swapped = swap_ffmpeg(L, ffmpeg_dir)

    trim(L)
    audit_info = audit(L)                               # before the notices: it can add the MSVC runtime
    rows = collect_licences(L, plat, cache, ffmpeg_dir, audit_info["vc_runtime_bundled"])
    precompile(L)
    smoke_info = smoke(L, work, a.mediapipe_model)
    manifest(L, plat, rows, swapped, audit_info, smoke_info)
    pack(L, plat, os.path.abspath(a.out))


if __name__ == "__main__":
    main()
