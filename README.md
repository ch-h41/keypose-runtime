# Keypose engine runtime

The Python runtime that the tracking engine of **Keypose** (an After Effects panel) runs on:
CPython 3.12 with NumPy, OpenCV, ONNX Runtime and MediaPipe, prebuilt and tested for each
platform and published here as release files.

The panel downloads the pack for your computer the first time it runs. It checks the pack's
sha256 against the value it shipped with before unpacking anything. Everyone runs the exact stack
that was tested, and nothing is installed from PyPI on your machine.

| Platform | Pack | Needs |
|---|---|---|
| macOS, Apple silicon | `keypose-runtime-<version>-darwin-arm64.tar.gz` | macOS 14 Sonoma or later |
| Windows, x64 | `keypose-runtime-<version>-win32-x64.tar.gz` | Windows 10 or 11 |

Intel Macs are not supported. ONNX Runtime stopped publishing Intel Mac builds after 1.23, and
MediaPipe has none at all.

## What is inside

| Component | Licence |
|---|---|
| CPython 3.12 ([python-build-standalone](https://github.com/astral-sh/python-build-standalone)) | PSF-2.0, plus the permissive licences of the libraries built into it (OpenSSL, SQLite, zlib, libffi, ...) |
| NumPy | BSD-3-Clause and bundled permissive licences |
| OpenCV (`opencv-python-headless`) | Apache-2.0, plus bundled libraries (see below for FFmpeg) |
| FFmpeg (the parts OpenCV uses to read video) | **LGPL-2.1-or-later** |
| ONNX Runtime, flatbuffers, protobuf, packaging | MIT, Apache-2.0, BSD-3-Clause, Apache-2.0 OR BSD-2-Clause |
| MediaPipe, absl-py | Apache-2.0 |
| certifi | MPL-2.0 |

The exact versions are pinned in [`requirements.in`](requirements.in) and
[`runtime.json`](runtime.json). Each is locked to a single file by sha256 in [`lock/`](lock).

**Every pack carries its own notices:** `THIRD-PARTY-NOTICES.txt` at the top of the pack lists
every component, version and licence, and the `licenses/` folder next to it holds the full texts.
The same notices file is attached to each release. [SOURCES.md](SOURCES.md) says where the
source of each component is. The LGPL source is attached to every release.

### The FFmpeg in the macOS pack

On macOS, the OpenCV wheel from PyPI bundles Homebrew's FFmpeg, which is built with GPL-only
components (x264, x265 and others) and so is licensed **GPL-3.0-or-later**. This project does not
ship it. The macOS build does three things:

1. It compiles FFmpeg from the official, signed source as **LGPL-2.1-or-later**
   ([`tools/ffmpeg.sh`](tools/ffmpeg.sh)).
2. It points OpenCV's `cv2` module at those libraries. Only the library references change; the
   OpenCV code is untouched.
3. It leaves out the 85 libraries that only the GPL FFmpeg needed.

The build then scans every native file and fails if anything GPL-licensed remains. On Windows,
OpenCV already uses an LGPL FFmpeg, in its own replaceable `opencv_videoio_ffmpeg*.dll`.

## Checking a download

Each release lists the sha256 of every file in `SHA256SUMS`.

```bash
shasum -a 256 keypose-runtime-*-darwin-arm64.tar.gz
```

```powershell
Get-FileHash keypose-runtime-*-win32-x64.tar.gz -Algorithm SHA256
```

## Installing offline

If the computer cannot reach github.com (studio networks often block it), download the pack for
your platform from the [releases](../../releases) on any machine. Then choose **Install from a
file…** on Keypose's setup screen and pick the downloaded file. The panel still checks the sha256
before installing.

## How a pack is built

[`tools/build.py`](tools/build.py) runs on the target platform in GitHub Actions
([workflow](.github/workflows/build.yml)). Any of these steps can fail the build:

1. Fetch the pinned CPython and check its sha256.
2. Install exactly the locked wheels (`--no-deps --require-hashes --only-binary`), then check that
   no dependency is missing except the ones [`runtime.json`](runtime.json) leaves out on purpose.
3. On macOS, swap in the LGPL FFmpeg as described above.
4. Remove what the engine never runs: pip, tkinter/IDLE, headers and the test suites inside NumPy and MediaPipe.
5. Collect every licence file into `licenses/` and write `THIRD-PARTY-NOTICES.txt`.
6. Audit every native library. Each dependency must resolve inside the pack or to the operating
   system. On Windows, the Visual C++ runtime is bundled when something needs it. Nothing may be
   GPL.
7. Precompile, then run [`tools/smoke.py`](tools/smoke.py) with the pack's own interpreter. The
   smoke test covers ONNX Runtime on the CPU and GPU, OpenCV image operations, video decoding with
   a frame seek, and a MediaPipe landmarker run. CI then unpacks the finished archive the way a
   customer's machine would and runs the smoke test again.
8. Archive, and record the sha256.

To build one yourself, use Python 3.14 or later on the matching platform:

```bash
python3 tools/build.py
```

## Making a new release

1. Change the pins in `requirements.in` (or the Python/FFmpeg pins in `runtime.json`).
2. Run `python3 tools/lock.py`, then review the lock diff like a code diff.
3. Bump `version` in `runtime.json` and push.
4. Tag it: `git tag v<version> && git push --tags`. CI builds both platforms, attaches the packs,
   notices, checksums and LGPL sources, and writes `engine-runtime.json`, the block the Keypose
   plugin pins in `helper/engine-runtime.json`.

## Licence

The scripts in this repository are under the [MIT licence](LICENSE). The software in the packs
is under the licences listed above; this licence does not cover it.
