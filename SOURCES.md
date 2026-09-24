# Sources

Where the source code of everything in a Keypose runtime pack comes from. Every component is
open source. The two licences that carry a source obligation are FFmpeg's (LGPL-2.1-or-later)
and certifi's (MPL-2.0), and both are covered below.

## LGPL: FFmpeg

**Each release attaches the complete corresponding source of the FFmpeg it ships**, so the
source is always available from the same place as the binaries.

| Pack | FFmpeg | Built by | Source attached to the release |
|---|---|---|---|
| macOS arm64 | 7.1.5, LGPL-2.1-or-later, shared libraries in `lib/python3.12/site-packages/cv2/.dylibs/` (`libavcodec`, `libavformat`, `libavutil`, `libswscale`, `libavdevice`) | this repository: [`tools/ffmpeg.sh`](tools/ffmpeg.sh) has the full configure line, and each pack has it again in `licenses/ffmpeg/BUILD.txt` | `ffmpeg-7.1.5.tar.xz`, the official release, signed by the FFmpeg release key `FCF986EA15E6E293A5644F10B4322F04D67658D8` |
| Windows x64 | 7.1 (tag `n7.1`), LGPL-2.1-or-later, statically linked into `Lib/site-packages/cv2/opencv_videoio_ffmpeg500_64.dll` | the OpenCV project, [opencv_3rdparty@06dc20c](https://github.com/opencv/opencv_3rdparty/tree/06dc20cad65dc7fcf784f70c95d46750520889a7/ffmpeg) | `ffmpeg-7.1.tar.xz` (official release, same key) and `opencv-videoio-ffmpeg-build-06dc20c.tar.gz` (OpenCV's build scripts for the DLL) |

You may replace these libraries with your own build:

- **macOS:** build the same FFmpeg major versions with `tools/ffmpeg.sh` or your own configure
  line, then copy `lib<name>.<major>.dylib` over the files in `cv2/.dylibs/`.
- **Windows:** build the DLL with OpenCV's scripts and put it in place of
  `opencv_videoio_ffmpeg500_64.dll`.

The Windows DLL also contains libvpx 1.16.0 and libaom 3.14.1, both BSD-licensed. Their
versions are in OpenCV's `download_src.sh`.

If a link here ever stops working, open an issue in this repository and the source will be
provided.

## MPL-2.0: certifi

certifi is pure Python. Its source is the `certifi/` folder in every pack (`core.py` and the
`cacert.pem` bundle), unmodified from the [PyPI release](https://pypi.org/project/certifi/)
([upstream](https://github.com/certifi/python-certifi)).

## Permissive components

These have no source obligation. Where they come from:

| Component | Binary used | Source |
|---|---|---|
| CPython 3.12.14 | [python-build-standalone 20260901](https://github.com/astral-sh/python-build-standalone/releases/tag/20260901) `install_only` build, sha256 pinned in `runtime.json` | [python.org](https://www.python.org/downloads/source/) and the [build scripts](https://github.com/astral-sh/python-build-standalone) |
| NumPy, OpenCV (`opencv-python-headless`), ONNX Runtime, MediaPipe, protobuf, flatbuffers, absl-py, packaging | the PyPI wheels named in [`lock/`](lock), sha256 pinned | each project's PyPI page links its repository; the version is the one in `requirements.in` |

## What the build changes

Nothing is modified except the following, and each change is written into the pack:

- **macOS, OpenCV:** `cv2.abi3.so` load commands now point to the LGPL FFmpeg above (changed
  with `install_name_tool`, then re-signed ad hoc). The libraries only the replaced GPL FFmpeg
  needed are removed. The full list is in `licenses/opencv-python-headless/KEYPOSE-CHANGES.txt`.
- **Removed:** pip, tkinter/IDLE/turtledemo/ensurepip, C headers, and the `test`/`tests` folders inside NumPy and MediaPipe.
- **Added:** `.pyc` files compiled from the shipped sources; on Windows, the Microsoft Visual C++
  runtime DLLs next to `python.exe` when a component needs them.
