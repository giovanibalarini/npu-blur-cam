#!/usr/bin/env python3
"""Duas cameras virtuais a partir de uma webcam: uma com desfoque de fundo na NPU
(Intel AI Boost) e uma de passagem. Trocar entre elas no seletor de camera do
Meet/Slack/Teams liga e desliga o desfoque, sem terminal.

  /dev/video0 --+--> [PPHumanSeg na NPU] --> compoe blur --> /dev/video9  "NPU Blur Cam"
                |
                +--> repassa o quadro cru -----------------> /dev/video10 "NPU Cam"

O que e calculado depende de quem esta consumindo cada device: sem consumidor no
device com blur, a NPU nao roda. Sem consumidor em nenhum dos dois, o processo
dorme e solta a webcam fisica.

Uso:
  ./blur_cam.py                    # dois devices, blur na NPU, 1280x720@30
  ./blur_cam.py --ov-device GPU    # compara com a iGPU
  ./blur_cam.py --no-raw           # so o device com blur, como era antes
  ./blur_cam.py --selftest         # sem camera e sem loopback
"""
import argparse, glob, os, subprocess, sys, time
import numpy as np, cv2, openvino as ov

MODEL = os.environ.get("NPU_BLUR_MODEL",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "pphumanseg.onnx"))
SIZE = 192  # entrada do modelo

ap = argparse.ArgumentParser()
ap.add_argument("--cam", default="/dev/video0")
ap.add_argument("--out", default="/dev/video9", help="device COM desfoque")
ap.add_argument("--out-raw", default="/dev/video10", help="device de passagem, sem desfoque")
ap.add_argument("--no-raw", action="store_true", help="nao publica o device de passagem")
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
                help="segundos sem consumidor em NENHUM device antes de dormir. 0 desliga")
ap.add_argument("--idle-fps", type=float, default=2.0,
                help="taxa mantida enquanto ocioso — os devices precisam continuar transmitindo, "
                     "senao o exclusive_caps os devolve a Video Output e eles somem da lista")
ap.add_argument("--selftest", action="store_true", help="frames sinteticos, sem camera/loopback")
a = ap.parse_args()

cv2.setNumThreads(a.cv_threads)

_core = ov.Core()
req = _core.compile_model(_core.read_model(MODEL), a.ov_device).create_infer_request()
print(f"modelo compilado em {a.ov_device} | OpenCV com {cv2.getNumThreads()} thread(s)", flush=True)


def fundo_borrado(bgr):
    """Desfoque do quadro inteiro. Serve de fundo na composicao e de placeholder
    seguro no device com blur quando ninguem o esta consumindo — assim trocar de
    device nunca expoe a imagem limpa, nem por um quadro."""
    small = cv2.resize(bgr, (a.width // 8, a.height // 8))
    return cv2.resize(cv2.GaussianBlur(small, (a.blur, a.blur), 0), (a.width, a.height))


def segment(bgr, state={}):
    # BGR->RGB e normalizacao para [-1,1] como o opencv_zoo faz; sem isso o modelo
    # recebe canais trocados e escala errada (mede ~0,3% da mascara, mas e de graca).
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    x = (cv2.resize(rgb, (SIZE, SIZE)).astype(np.float32) / 255.0 - 0.5) / 0.5
    logits = req.infer({0: x.transpose(2, 0, 1)[None]})[0][0]   # (2, 192, 192)
    m = (logits[1] > logits[0]).astype(np.float32)              # 1 = pessoa
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


def compose(bgr, mask, bg=None):
    if bg is None:
        bg = fundo_borrado(bgr)
    if a.compose == "float":
        return (bgr * mask + bg * (1 - mask)).astype(np.uint8)
    return cv2.add(cv2.multiply(bgr, mask, scale=1 / 255.),
                   cv2.multiply(bg, cv2.bitwise_not(mask), scale=1 / 255.))


def consumidores(devices):
    """{device: set(pids)} — quem tem cada device aberto, excluindo nos mesmos."""
    eu = os.getpid()
    achados = {d: set() for d in devices}
    for fd in glob.glob("/proc/[0-9]*/fd/*"):
        try:
            pid = int(fd.split("/", 3)[2])
            if pid == eu:
                continue
            alvo = os.readlink(fd)
            if alvo in achados:
                achados[alvo].add(pid)
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


def run_selftest():
    frame = (np.random.rand(a.height, a.width, 3) * 255).astype(np.uint8)
    n, t0 = 0, time.perf_counter()
    for _ in range(150):
        compose(frame, segment(frame))
        n += 1
        if n % 60 == 0:
            dt = time.perf_counter() - t0
            print(f"  {n:5d} frames | {n/dt:5.1f} fps | {dt/n*1000:5.2f} ms/frame", flush=True)


def run_servico(cam_blur, cam_raw):
    """Publica os dois devices e calcula so o que alguem esta consumindo.

    Ocioso (ninguem em nenhum device) custa quase nada: nao le a webcam, nao infere,
    nao compoe, e solta o /dev/video0 para outros aplicativos. Mas continua repetindo
    o ultimo quadro na taxa minima, porque parar de transmitir faria o exclusive_caps
    devolver os devices a Video Output — eles sumiriam da lista de cameras do
    navegador sem disparar devicechange, e so um F5 os traria de volta.
    """
    devs = [a.out] + ([a.out_raw] if cam_raw else [])
    cap = None
    ult_blur = ult_raw = None
    ocioso = False
    visto = time.monotonic()
    proximo_check = 0.0
    cons = {d: set() for d in devs}
    n, t0 = 0, time.perf_counter()
    # devices sem consumidor so precisam de quadros suficientes para nao voltarem a
    # Video Output; mandar a 30 fps para ninguem paga conversao RGB->I420 a toa.
    prox_ocioso = {d: 0.0 for d in devs}
    intervalo = 1.0 / a.fps
    espera_ocioso = 1.0 / max(a.idle_fps, .1)
    estado_anterior = None

    while True:
        quadro_em = time.monotonic()

        if quadro_em >= proximo_check:
            proximo_check = quadro_em + 1.0
            cons = consumidores(devs)
            algum = any(cons.values())
            if algum:
                visto = quadro_em
                if ocioso:
                    cap = abrir_camera()
                    if cap is None:
                        print("  consumidor chegou, mas a camera esta ocupada", flush=True)
                        proximo_check = quadro_em + 2.0
                    else:
                        ocioso = False
                        n, t0 = 0, time.perf_counter()
            elif not ocioso and a.idle_after and quadro_em - visto > a.idle_after:
                ocioso = True
                if cap is not None:
                    cap.release(); cap = None
                print(f"  sem consumidor ha {a.idle_after:.0f}s — dormindo e soltando {a.cam}",
                      flush=True)

            if not ocioso:
                estado = ("blur" if cons.get(a.out) else "") + ("raw" if cons.get(a.out_raw) else "")
                if estado != estado_anterior:
                    rotulos = {"blur": "so o device COM desfoque",
                               "raw": "so o device de PASSAGEM (NPU parada)",
                               "blurraw": "os dois devices"}
                    print(f"  consumidor em: {rotulos.get(estado, 'nenhum')}", flush=True)
                    estado_anterior = estado
                    n, t0 = 0, time.perf_counter()

        if ocioso:
            estado_anterior = None
            if ult_blur is not None:
                cam_blur.send(ult_blur)
            if cam_raw and ult_raw is not None:
                cam_raw.send(ult_raw)
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

        # passagem: barato, so troca de espaco de cor
        if cam_raw:
            if cons.get(a.out_raw):
                ult_raw = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                cam_raw.send(ult_raw)
            elif quadro_em >= prox_ocioso[a.out_raw]:
                prox_ocioso[a.out_raw] = quadro_em + espera_ocioso
                if ult_raw is None:
                    ult_raw = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                cam_raw.send(ult_raw)

        # blur: so gasta NPU se alguem estiver olhando. Sem consumidor, manda o
        # quadro inteiro desfocado — nunca a imagem limpa.
        if cons.get(a.out):
            bg = fundo_borrado(bgr)
            ult_blur = cv2.cvtColor(compose(bgr, segment(bgr), bg), cv2.COLOR_BGR2RGB)
            cam_blur.send(ult_blur)
        elif quadro_em >= prox_ocioso[a.out]:
            prox_ocioso[a.out] = quadro_em + espera_ocioso
            ult_blur = cv2.cvtColor(fundo_borrado(bgr), cv2.COLOR_BGR2RGB)
            cam_blur.send(ult_blur)

        n += 1
        if n % 60 == 0:
            dt = time.perf_counter() - t0
            print(f"  {n:5d} frames | {n/dt:5.1f} fps | {dt/n*1000:5.2f} ms/frame", flush=True)

        resto = intervalo - (time.monotonic() - quadro_em)
        if resto > 0:
            time.sleep(resto)


if a.selftest:
    run_selftest()
else:
    import pyvirtualcam
    from contextlib import ExitStack
    with ExitStack() as pilha:
        cam_blur = pilha.enter_context(pyvirtualcam.Camera(
            a.width, a.height, a.fps, device=a.out, fmt=pyvirtualcam.PixelFormat.RGB))
        cam_raw = None
        if not a.no_raw:
            try:
                cam_raw = pilha.enter_context(pyvirtualcam.Camera(
                    a.width, a.height, a.fps, device=a.out_raw, fmt=pyvirtualcam.PixelFormat.RGB))
            except Exception as e:
                print(f"  aviso: nao abri {a.out_raw} ({e}); seguindo so com o device de blur",
                      flush=True)
        alvos = cam_blur.device + (f" e {cam_raw.device}" if cam_raw else "")
        print(f"publicando {alvos}", flush=True)
        print("  escolha 'NPU Blur Cam' para desfoque, 'NPU Cam' para imagem limpa", flush=True)
        run_servico(cam_blur, cam_raw)
