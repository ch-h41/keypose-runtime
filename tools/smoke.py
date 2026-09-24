"""Proves a built runtime works before it is packed. tools/build.py runs it with the pack's OWN
interpreter, isolated from the build machine's environment:

    <runtime>/bin/python3 -E -s tools/smoke.py <result.json> [holistic_landmarker.task]

Exercises what the Keypose engine actually uses: numpy, ONNX Runtime on the CPU and on the GPU
provider when there is one, OpenCV image operations and NMS, FFmpeg video decode with a frame seek,
and MediaPipe's native library (a full landmarker run when a model file is given). Exits non-zero
on the first failure; the JSON records versions and timings for the build log.
"""
import base64, json, os, sys, tempfile, time, types

RESULT = {"ok": False, "checks": {}}
# Each @check runs as soon as it is defined, top to bottom, so helpers go above the checks using them.


def check(name):
    def wrap(fn):
        t = time.perf_counter()
        try:
            RESULT["checks"][name] = {"ok": True, "info": fn(), "ms": round((time.perf_counter() - t) * 1000, 1)}
        except Exception as e:
            RESULT["checks"][name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            finish(1)
        return fn
    return wrap


def finish(code):
    RESULT["ok"] = code == 0
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        json.dump(RESULT, f, indent=1)
    print(json.dumps(RESULT, indent=1))
    sys.exit(code)


@check("interpreter")
def _():
    assert sys.version_info[:2] == (3, 12), sys.version
    assert not sys.flags.no_site and sys.flags.ignore_environment and sys.flags.no_user_site, "run with -E -s"
    here = os.path.realpath(sys.prefix)
    for p in sys.path[1:]:      # [0] is this script's folder
        if p and os.path.exists(p):
            assert os.path.realpath(p).startswith(here), f"sys.path reaches outside the runtime: {p}"
    return {"version": sys.version.split()[0], "prefix": sys.prefix}


@check("numpy")
def _():
    import numpy as np
    a = np.arange(12, dtype=np.float32).reshape(3, 4)
    assert float((a @ a.T).sum()) == 1134.0          # (sum of the rows) . (sum of the rows)
    return np.__version__


# 220 bytes: Y = Relu(X @ W + B), opset 17 - made with the onnx package, embedded so this needs nothing else
MODEL = base64.b64decode(
    "CAgSFWtleXBvc2UtcnVudGltZS1zbW9rZTq6AQoRCgFYCgFXEgFNIgZNYXRNdWwKDgoBTQoBQhIBQSIDQWRkCgwKAUESAVkiBFJlbHUSBXNtb2tl"
    "KjsIAwgEEAFCAVdKMAAAAADNzMw9zcxMPpqZmT7NzMw+AAAAP5qZGT8zMzM/zcxMP2ZmZj8AAIA/zcyMPyoZCAQQAUIBQkoQAACAvwAAAAAAAIA/"
    "AAAAQFoTCgFYEg4KDAgBEggKAggBCgIIA2ITCgFZEg4KDAgBEggKAggBCgIIBEIECgAQEQ==")


@check("onnxruntime")
def _():
    import numpy as np, onnxruntime as ort
    x = np.array([[1, 2, 3]], np.float32)
    want = np.array([[2.2, 3.8, 5.4, 7.0]], np.float32)
    out = {"version": ort.__version__, "providers": ort.get_available_providers()}
    cpu = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"]).run(None, {"X": x})[0]
    assert np.allclose(cpu, want, atol=1e-5), cpu
    for ep in ("CoreMLExecutionProvider", "DmlExecutionProvider", "CUDAExecutionProvider"):
        if ep in out["providers"]:
            so = ort.SessionOptions()
            if ep == "DmlExecutionProvider":
                so.enable_mem_pattern = False
                so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            gpu = ort.InferenceSession(MODEL, so, providers=[ep, "CPUExecutionProvider"]).run(None, {"X": x})[0]
            assert np.allclose(gpu, want, atol=1e-3), (ep, gpu)
            out["gpu"] = ep
    return out


@check("opencv")
def _():
    import cv2, numpy as np
    img = np.zeros((480, 640, 3), np.uint8)
    cv2.rectangle(img, (100, 100), (300, 400), (40, 180, 220), -1)
    small = cv2.resize(img, (320, 240), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    M = cv2.getAffineTransform(np.float32([[0, 0], [1, 0], [0, 1]]), np.float32([[0, 0], [2, 0], [0, 2]]))
    warped = cv2.warpAffine(gray, M, (640, 480))
    assert warped.shape == (480, 640) and int(warped[300, 300]) > 0
    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok and cv2.imdecode(jpg, cv2.IMREAD_COLOR).shape == img.shape
    keep = cv2.dnn.NMSBoxes([[0, 0, 10, 10], [1, 1, 10, 10], [50, 50, 10, 10]], [0.9, 0.8, 0.7], 0.3, 0.5)
    assert sorted(int(i) for i in np.array(keep).ravel()) == [0, 2], keep
    return cv2.__version__


def ffmpeg_license():
    """macOS: the FFmpeg that cv2 loads must be the LGPL build this runtime puts in its place."""
    import ctypes, glob, cv2
    libs = glob.glob(os.path.join(os.path.dirname(cv2.__file__), ".dylibs", "libavcodec.*.dylib"))
    if not libs:
        return None
    lib = ctypes.CDLL(libs[0])
    lib.avcodec_license.restype = ctypes.c_char_p
    return lib.avcodec_license().decode()


@check("video")
def _():
    import cv2, numpy as np
    d = tempfile.mkdtemp(prefix="keypose-smoke-")
    path = os.path.join(d, "clip.mp4")
    w = cv2.VideoWriter(path, cv2.CAP_FFMPEG, cv2.VideoWriter_fourcc(*"mp4v"), 25, (320, 240))
    assert w.isOpened(), "no FFmpeg video writer"
    for i in range(30):                      # frame i carries its own number as a grey level
        w.write(np.full((240, 320, 3), 8 * i, np.uint8))
    w.release()
    cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG)
    assert cap.isOpened(), "FFmpeg could not open the clip it just wrote"
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames = 0
    while cap.read()[0]:
        frames += 1
    cap.set(cv2.CAP_PROP_POS_FRAMES, 20)     # the engine seeks: it must land on the right frame
    ok, f = cap.read()
    cap.release()
    os.remove(path); os.rmdir(d)
    assert n == 30 and frames == 30, (n, frames)
    assert ok and abs(float(f.mean()) - 160) < 6, f.mean() if ok else "no frame after seek"
    info = {"backend": "FFMPEG", "frames": frames}
    lic = ffmpeg_license()
    if lic:
        info["ffmpeg_license"] = lic
        assert lic.startswith("LGPL"), f"OpenCV's FFmpeg is {lic}"
    return info


@check("mediapipe")
def _():
    # the engine stubs matplotlib the same way (helper/mp_engine.py): MediaPipe's vision package
    # imports it for drawing helpers only, and the runtime leaves it out
    for n in ("matplotlib", "matplotlib.pyplot"):
        sys.modules.setdefault(n, types.ModuleType(n))
    import numpy as np, mediapipe as mp
    from mediapipe.tasks.python import BaseOptions
    from mediapipe.tasks.python.vision import HolisticLandmarker, HolisticLandmarkerOptions
    from mediapipe.tasks.python.vision.core.vision_task_running_mode import VisionTaskRunningMode
    img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.zeros((256, 256, 3), np.uint8))
    info = {"version": mp.__version__, "image": [img.width, img.height]}
    if len(sys.argv) > 2:
        opts = HolisticLandmarkerOptions(base_options=BaseOptions(model_asset_path=sys.argv[2]),
                                         running_mode=VisionTaskRunningMode.IMAGE)
        with HolisticLandmarker.create_from_options(opts) as lm:
            r = lm.detect(img)
        info["landmarker"] = "ran"
        info["people_in_blank_image"] = len(r.pose_landmarks)
    return info


if __name__ == "__main__":
    finish(0)
