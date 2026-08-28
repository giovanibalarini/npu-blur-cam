#!/usr/bin/env python3
"""Camera virtual com desfoque de fundo, segmentacao rodando na NPU (Intel AI Boost).

  /dev/video0 --> [PPHumanSeg na NPU] --> compoe blur --> /dev/video9 (v4l2loopback)

Uso:
  ./blur_cam.py                    # blur, NPU, 1280x720@30
  ./blur_cam.py --ov-device GPU    # compara com a iGPU
  ./blur_cam.py --selftest         # sem camera e sem loopback
"""
import argparse, glob, os, subprocess, sys, time
import numpy as np, cv2, openvino as ov

MODEL = os.environ.get("NPU_BLUR_MODEL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "pphumanseg.onnx"))
SIZE = 192  # entrada do modelo

ap = argparse.ArgumentParser()
ap.add_argument("--cam", default="/dev/video0")
ap.add_argument("--out", default="/dev/video9")
ap.add_argument("--width", type=int, default=1280)
ap.add_argument("--height", type=int, default=720)
ap.add_argument("--fps", type=int, default=30)
ap.add_argument("--ov-device", default="NPU", help="NPU, GPU ou CPU")
ap.add_argument("--blur", type=int, default=21, help="forca do desfoque (impar)")
ap.add_argument("--smooth", type=float, default=0.6, help="EMA da mascara: 0=sem, 0.9=muito")
ap.add_argument("--compose", default="fast", choices=("fast", "float"),
                help="fast = feather em 192x192 + uint8; float = feather em 720p + float32")
ap.add_argument("--cv-threads", type=int, default=1,
                help="threads do OpenCV. 1 e o certo: o pool paraleliza operacoes pequenas e "
                     "gasta 43%% mais CPU pela MESMA latencia de parede (medido). 0 = automatico")
ap.add_argument("--idle-after", type=float, default=5.0,
                help="segundos sem consumidor no /dev/video9 antes de dormir. 0 desliga o modo ocioso")
ap.add_argument("--idle-fps", type=float, default=2.0,
                help="taxa mantida enquanto ocioso — o device precisa continuar transmitindo, "
                     "senao o exclusive_caps o devolve a Video Output e ele some da lista de cameras")
ap.add_argument("--selftest", action="store_true", help="frames sinteticos, sem camera/loopback")
a = ap.parse_args()

cv2.setNumThreads(a.cv_threads)

req = ov.Core().compile_model(ov.Core().read_model(MODEL), a.ov_device).create_infer_request()
print(f"modelo compilado em {a.ov_device} | OpenCV com {cv2.getNumThreads()} thread(s)")

def segment(bgr, state={}):
    blob = cv2.resize(bgr, (SIZE, SIZE)).transpose(2, 0, 1)[None].astype(np.float32) / 255.0
    logits = req.infer({0: blob})[0][0]              # (2, 192, 192)
    m = (logits[1] > logits[0]).astype(np.float32)   # 1 = pessoa
    prev = state.get("m")
    if prev is not None and a.smooth > 0:            # EMA: mata o tremor de borda
        m = a.smooth * prev + (1 - a.smooth) * m
    state["m"] = m
    if a.compose == "float":                              # feather caro, em 720p
        m = cv2.resize(m, (a.width, a.height), interpolation=cv2.INTER_LINEAR)
        return cv2.GaussianBlur(m, (15, 15), 0)[:, :, None]
    m = cv2.GaussianBlur(m, (9, 9), 0)                    # feather barato, ainda em 192x192
    m8 = cv2.resize((m * 255).astype(np.uint8), (a.width, a.height), interpolation=cv2.INTER_LINEAR)
    return cv2.cvtColor(m8, cv2.COLOR_GRAY2BGR)

def compose(bgr, mask):
    small = cv2.resize(bgr, (a.width // 8, a.height // 8))
    bg = cv2.resize(cv2.GaussianBlur(small, (a.blur, a.blur), 0), (a.width, a.height))
    if a.compose == "float":
        return (bgr * mask + bg * (1 - mask)).astype(np.uint8)
    return cv2.add(cv2.multiply(bgr, mask, scale=1 / 255.),
                   cv2.multiply(bg, cv2.bitwise_not(mask), scale=1 / 255.))

def consumidores():
    """PIDs, alem do nosso, com o device de saida aberto."""
    eu, achados = os.getpid(), set()
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            pid = int(fd.split("/", 3)[2])
            if pid != eu and os.readlink(fd) == a.out:
                achados.add(pid)
        except (OSError, ValueError):
            pass
    return achados


def abrir_camera():
    cap = cv2.VideoCapture(a.cam, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, a.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, a.height)
    cap.set(cv2.CAP_PROP_FPS, a.fps)
    if not cap.isOpened():
        return None
    # Sem isto a webcam corta a taxa pela metade no escuro para expor mais tempo:
    # 15 fps em vez de 30. O padrao do controle e 0, mas algo o liga.
    subprocess.run(["v4l2-ctl", "-d", a.cam, "--set-ctrl", "exposure_dynamic_framerate=0"],
                   capture_output=True)
    return cap


def frames():
    if a.selftest:
        f = (np.random.rand(a.height, a.width, 3) * 255).astype(np.uint8)
        for _ in range(150):
            yield f
        return
    cap = cv2.VideoCapture(a.cam, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, a.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, a.height)
    cap.set(cv2.CAP_PROP_FPS, a.fps)
    if not cap.isOpened():
        sys.exit(f"nao consegui abrir {a.cam}")
    while True:
        ok, f = cap.read()
        if not ok:
            break
        yield f

def run(sink=None):
    n, t0 = 0, time.perf_counter()
    for bgr in frames():
        if bgr.shape[:2] != (a.height, a.width):
            bgr = cv2.resize(bgr, (a.width, a.height))
        out = compose(bgr, segment(bgr))
        if sink:
            sink.send(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
            sink.sleep_until_next_frame()
        n += 1
        if n % 60 == 0:
            dt = time.perf_counter() - t0
            print(f"  {n:5d} frames | {n/dt:5.1f} fps | {dt/n*1000:5.2f} ms/frame", flush=True)

def run_servico(sink):
    """Igual ao run(), mas solta a camera quando ninguem esta consumindo.

    Ocioso custa quase nada: nao le a webcam, nao infere, nao compoe. So repete o
    ultimo quadro na taxa minima, porque parar de transmitir faria o exclusive_caps
    devolver o device a Video Output — ele sumiria da lista de cameras do navegador
    sem disparar devicechange, e so um F5 o traria de volta.
    """
    cap, ultimo, ocioso = None, None, False
    n, t0, visto = 0, time.perf_counter(), time.monotonic()
    proximo_check = 0.0
    espera_ocioso = 1.0 / max(a.idle_fps, .1)

    while True:
        agora = time.monotonic()
        if agora >= proximo_check:
            proximo_check = agora + 1.0
            if consumidores():
                visto = agora
                if ocioso:
                    cap = abrir_camera()
                    if cap is None:
                        print("  consumidor chegou, mas a camera esta ocupada", flush=True)
                        proximo_check = agora + 2.0
                    else:
                        ocioso = False
                        n, t0 = 0, time.perf_counter()
                        print("  consumidor detectado — acordando", flush=True)
            elif not ocioso and a.idle_after and agora - visto > a.idle_after:
                ocioso = True
                if cap is not None:
                    cap.release(); cap = None
                print(f"  sem consumidor ha {a.idle_after:.0f}s — dormindo e soltando {a.cam}", flush=True)

        if ocioso:
            if ultimo is not None:
                sink.send(ultimo)
            time.sleep(espera_ocioso)
            continue

        if cap is None:
            cap = abrir_camera()
            if cap is None:
                time.sleep(2.0); continue

        ok, bgr = cap.read()
        if not ok:
            cap.release(); cap = None; time.sleep(.5); continue
        if bgr.shape[:2] != (a.height, a.width):
            bgr = cv2.resize(bgr, (a.width, a.height))
        out = compose(bgr, segment(bgr))
        ultimo = cv2.cvtColor(out, cv2.COLOR_BGR2RGB)
        sink.send(ultimo)
        sink.sleep_until_next_frame()
        n += 1
        if n % 60 == 0:
            dt = time.perf_counter() - t0
            print(f"  {n:5d} frames | {n/dt:5.1f} fps | {dt/n*1000:5.2f} ms/frame", flush=True)



if a.selftest:
    run()
else:
    import pyvirtualcam
    with pyvirtualcam.Camera(a.width, a.height, a.fps, device=a.out,
                             fmt=pyvirtualcam.PixelFormat.RGB) as cam:
        print(f"camera virtual em {cam.device} — escolha '{cam.device}' no Meet/Slack. Ctrl+C encerra.")
        run_servico(cam) if a.idle_after else run(cam)
