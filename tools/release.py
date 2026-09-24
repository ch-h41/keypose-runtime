#!/usr/bin/env python3
"""Assembles a release from the platform builds in dist/ (CI runs it after every build job passed).

    python3 tools/release.py dist

Writes RELEASE-NOTES.md (the release page text, not an asset) and into dist/:
  engine-runtime.json   the block the Keypose plugin pins (helper/engine-runtime.json): URL, sha256
                        and size of each platform's pack
  SHA256SUMS            every file in the release
  + the corresponding source of the LGPL components (runtime.json "sources"), downloaded and
    verified here so each release carries its own copy
"""
import hashlib, io, json, os, re, sys, tarfile, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = json.load(open(os.path.join(ROOT, "runtime.json"), encoding="utf-8"))
REPO = os.environ.get("GITHUB_REPOSITORY", "ch-h41/keypose-runtime")
TAG = "v" + CFG["version"]


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def sources(out):
    rows = []
    for s in CFG["sources"]:
        path = os.path.join(out, s["file"])
        if "url" in s:
            data = get(s["url"])
            if sha256_bytes(data) != s["sha256"]:
                sys.exit(f"ERROR: {s['url']} does not match runtime.json")
            open(path, "wb").write(data)
        else:                                   # a folder of a GitHub repo at a fixed commit
            gh = {"Accept": "application/vnd.github+json"}
            if os.environ.get("GITHUB_TOKEN"):
                gh["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
            tree = json.loads(get(f"https://api.github.com/repos/{s['github']}/git/trees/{s['commit']}?recursive=1", gh))["tree"]
            with tarfile.open(path, "w:gz") as t:
                for e in tree:
                    if e["type"] == "blob" and e["path"].startswith(s["path"]) and not re.search(s["skip"], e["path"]):
                        data = get(f"https://raw.githubusercontent.com/{s['github']}/{s['commit']}/{e['path']}")
                        ti = tarfile.TarInfo(e["path"]); ti.size = len(data); ti.mode = 0o644
                        t.addfile(ti, io.BytesIO(data))
        rows.append((s["file"], s["for"]))
    return rows


def main():
    out = sys.argv[1]
    entries = {}
    for plat in CFG["platforms"]:
        p = os.path.join(out, plat + ".json")
        if not os.path.exists(p):
            sys.exit(f"ERROR: no build for {plat} in {out}")
        e = json.load(open(p, encoding="utf-8"))
        e["url"] = f"https://github.com/{REPO}/releases/download/{TAG}/{e['file']}"
        entries[plat] = e
        os.remove(p)
    manifest = {"version": CFG["version"], "source": f"https://github.com/{REPO}", "platforms": entries}
    with open(os.path.join(out, "engine-runtime.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=1)
        f.write("\n")
    src = sources(out)
    with open("RELEASE-NOTES.md", "w", encoding="utf-8", newline="\n") as f:
        f.write(f"The Python runtime for the Keypose tracking engine, {CFG['version']}.\n\n"
                "The Keypose panel downloads the pack for your computer and checks its sha256 against the one it was "
                "released with; you never need to fetch these by hand unless installing offline.\n\n"
                "| Platform | File | Size | sha256 |\n|---|---|---|---|\n")
        for plat, e in entries.items():
            f.write(f"| {plat} (minimum OS {e['min_os']}) | `{e['file']}` | {e['size'] / 1e6:.0f} MB | `{e['sha256']}` |\n")
        f.write("\nThird-party licences: each pack carries `THIRD-PARTY-NOTICES.txt` and a `licenses/` folder; the "
                "notices are also attached here per platform.\n\nCorresponding source for the LGPL components:\n\n")
        for name, what in src:
            f.write(f"- `{name}` - {what}\n")
        f.write(f"\nHow these were built: [SOURCES.md](https://github.com/{REPO}/blob/{TAG}/SOURCES.md).\n")
    sums = []
    for n in sorted(os.listdir(out)):
        if n.endswith(".sha256") or n == "SHA256SUMS" or not os.path.isfile(os.path.join(out, n)):
            continue
        sums.append(f"{sha256_bytes(open(os.path.join(out, n), 'rb').read())}  {n}")
    for n in os.listdir(out):
        if n.endswith(".sha256"):
            os.remove(os.path.join(out, n))
    open(os.path.join(out, "SHA256SUMS"), "w", encoding="utf-8", newline="\n").write("\n".join(sums) + "\n")
    print(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
