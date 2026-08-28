# npu-blur-cam

Desfoque de fundo para videochamada com a segmentação rodando na **NPU Intel
(AI Boost)**, servindo qualquer aplicativo — Meet, Slack, Teams — por uma câmera
virtual V4L2.

```
/dev/video0 (webcam) ──► blur_cam.py ──► /dev/video9 (v4l2loopback) ──► navegador
                            │   ▲
                            ▼   │  PPHumanSeg 192×192
                          NPU (Intel AI Boost)
```

Validado em Debian 13 (trixie), kernel 7.1.8, Core Ultra 9 288V (Lunar Lake).

## Por que isso existe

A NPU de um notebook Intel moderno normalmente não faz nada. **O Ollama não a
usa** — o backend dele é o `llama.cpp`, que tem CUDA, ROCm, Metal, Vulkan e
SYCL, e nenhum backend de NPU. Na prática o único caminho pronto é OpenVINO
sobre Level-Zero, e não há muita coisa pronta em cima dele.

Desfoque de fundo é uma carga que justifica o silício: contínua, com um modelo
pequeno o bastante para caber bem na NPU, e substituindo um recurso que hoje
roda dentro do navegador sobre CPU e GPU.

## Números medidos

Custo de CPU do passo de segmentação por frame, a 1280×720:

| Inferência em | Parede | CPU/frame | % de 1 core @30fps |
|---|---|---|---|
| **NPU** | 3,07 ms | **5,30 ms** | 15,9% |
| iGPU Arc 140V | 3,05 ms | 7,31 ms | 21,9% |
| CPU | 5,64 ms | 19,52 ms | 58,6% |

NPU e iGPU empatam em latência; a NPU ganha em CPU gasta para orquestrar.

**Cuidado com esses percentuais:** eles são só do passo de segmentação. O
processo inteiro custa **65,6% de um core** a 720p30 — o resto é decode MJPG,
conversão BGR→RGB e escrita no loopback. Para decidir se vale rodar isto na sua
máquina, o número é 66%, não 16%.

Em operação: **29,8 fps**, 33,5 ms por frame, contador de ocupação da NPU entre
3% e 4%. A chamada de inferência leva 0,94 ms de parede, 0,78 ms de engine
ocupada. O gargalo é a câmera a 30 fps, não o acelerador.

Composição importa mais que o modelo: feather da máscara em 192×192 com
aritmética `uint8` custa 1,30 ms; o mesmo em 720p com `float32` custa 7,07 ms —
5,4× de diferença. É a flag `--compose fast|float`.

## Pré-requisitos

Estes dois não são instalados pelo `install.sh`, porque ambos têm passos
interativos ou dependem da versão exata do seu kernel.

**1. Driver de userspace da NPU.** Não está nos repositórios do Debian; o
`apt install` não acha nada e não reclama. Baixe os `.deb` de
[intel/linux-npu-driver](https://github.com/intel/linux-npu-driver) (os builds
`ubuntu24.04` instalam limpo no trixie): `intel-level-zero-npu`,
`intel-driver-compiler-npu`, `intel-fw-npu`. Confira depois:

```bash
ls /dev/accel/accel0                 # existe?
groups | grep -q render || echo "adicione seu usuario ao grupo render"
```

A biblioteca chama-se `libze_intel_npu.so` — procurar por `libze_intel_vpu.so`,
como dizem receitas antigas, dá falso negativo.

**2. v4l2loopback 0.15.4 ou mais novo.** A 0.15.0 do Debian **não compila** em
kernel ≥ 7.0 e, se você aplicar patches para compilar, ela derruba o kernel no
primeiro `VIDIOC_QUERYCAP`. Use o upstream. Em Secure Boot, o módulo precisa da
chave MOK aceita no firmware. Veja [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Instalação

```bash
git clone https://github.com/giovanibalarini/npu-blur-cam
cd npu-blur-cam
./scripts/fetch-model.sh          # baixa o PPHumanSeg do opencv_zoo
sudo ./install.sh                 # /opt/npu-blur-cam + nputop + unit + modprobe.d
```

Depois, na sessão de **cada** usuário, sem root:

```bash
systemctl --user daemon-reload
systemctl --user enable --now npu-blur-cam
```

No Meet ou Slack, escolha a câmera **"NPU Blur Cam"**.

### Sem instalar nada no sistema

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./scripts/fetch-model.sh
./venv/bin/python blur_cam.py --selftest      # nao precisa de camera nem loopback
```

## Uso

```
blur_cam.py                      # blur na NPU, 1280x720@30
blur_cam.py --ov-device GPU      # compara com a iGPU
blur_cam.py --ov-device CPU      # compara com a CPU
blur_cam.py --compose float      # feather caro em 720p (5,4x mais lento)
blur_cam.py --blur 31            # desfoque mais forte
blur_cam.py --selftest           # frames sinteticos, sem camera nem loopback
```

## nputop

Não existe `intel_npu_top`. O `nvtop` e o `intel_gpu_top` varrem `/dev/dri` e
mostram só a iGPU. O `nputop` deste repo lê o sysfs do `intel_vpu`:

```
$ nputop --once
util 3.9%   freq 950/1950 MHz   mem 94.6 MiB   sched HW   busy 112.8s
   7466  giovani   33.0 MiB  /opt/npu-blur-cam/venv/bin/python .../blur_cam.py
```

`nputop` (TUI), `--once` (uma amostra), `--csv` (streaming). Sem dependências
além da stdlib.

**Limitação do kernel, não da ferramenta:** o `fdinfo` do `intel_vpu` publica
memória por processo, mas não tem campos `drm-engine-*`. Não existe tempo de NPU
por processo — utilização só global.

## Limitações conhecidas

- **Um usuário por vez.** Existe uma câmera física e um `/dev/video9`. Enquanto
  o serviço roda, ele segura a webcam em exclusivo e nenhum outro app a alcança.
- **Ordem importa:** suba o pipeline antes do navegador. Se o Chrome pegar a
  webcam primeiro, o `blur_cam.py` morre com `nao consegui abrir /dev/video0`.
- **Nunca derrube o produtor com o navegador segurando a câmera** — a câmera
  virtual some da lista e só volta com um reload da aba. Explicado no
  TROUBLESHOOTING.
- Testado só em Lunar Lake. Deve funcionar em Meteor Lake e Arrow Lake, sem
  verificação.

## Licença

MIT. O modelo PPHumanSeg não é versionado aqui e tem a licença do
[opencv_zoo](https://github.com/opencv/opencv_zoo).
