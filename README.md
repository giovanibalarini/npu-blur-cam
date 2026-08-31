# npu-blur-cam

Desfoque de fundo para videochamada com a segmentação rodando na **NPU Intel
(AI Boost)**, servindo qualquer aplicativo — Meet, Slack, Teams — por uma câmera
virtual V4L2.

```
                    ┌──► [PPHumanSeg na NPU] ──► compõe blur ──► /dev/video9   "NPU Blur Cam"
/dev/video0 ────────┤
   (webcam)         └──► repassa o quadro cru ─────────────────► /dev/video10  "NPU Cam"
```

**Dois devices, e é assim que se desliga o desfoque.** Trocar entre "NPU Blur Cam" e
"NPU Cam" no seletor de câmera do Meet, Slack ou Teams liga e desliga o blur — sem
terminal, sem reiniciar, sem recarregar a aba. Os dois saem da mesma leitura da
webcam, então alternar não disputa o `/dev/video0`.

O botão de desfoque do **próprio Meet não funciona** para isso, e não tem conserto:
ele atua sobre os quadros que já chegaram, e não tem como desfazer um desfoque que
veio pronto na imagem. Por isso o seletor de câmera é o interruptor.

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

Todas as medições abaixo são a 1280×720, com `--cv-threads 1` (o padrão) e o
pré-processamento corrigido. Onde a condição for outra, está dito.

### Onde a inferência roda

Pipeline completo, uma corrida por destino:

| Inferência em | CPU/frame | Parede | % de 1 núcleo @30fps |
|---|---|---|---|
| **NPU** | **7,28 ms** | 8,45 ms | **22%** |
| iGPU Arc 140V | 8,29 ms | 8,23 ms | 25% |
| CPU | 21,35 ms | 10,90 ms | 64% |

A NPU ganha da CPU por 42 pontos. Da iGPU, por três — praticamente empate. O
argumento a favor da NPU não é velocidade: é que ela é o único bloco do chip que
mais ninguém quer. Durante uma chamada, o processo gráfico do Chrome fica em ~22%
de um núcleo e o Meet inteiro em ~74%; a NPU fica em 4% e sem fila.

### Custo do processo, em operação real

Os 22% acima são o trabalho de CPU por quadro, medido isolado. O processo inteiro
custa mais, porque inclui a conversão RGB→I420 do `pyvirtualcam` e a escrita nos
devices:

| Estado | CPU do processo | NPU |
|---|---|---|
| consumidor em `NPU Blur Cam` | **45,6%** de um núcleo | ativa |
| consumidor em `NPU Cam` | 32,3% | parada |
| ninguém | **3,8%** | parada, webcam solta |

**Para decidir se vale rodar isto na sua máquina, o número é 45,6%** durante a
chamada e 3,8% no resto do tempo — não os 22% do benchmark.

### Decomposição por etapa

| Etapa | CPU/frame | Fatia |
|---|---|---|
| Decode MJPG da câmera | 4,74 ms | 65% |
| `compose()` do blur | 1,47 ms | 20% |
| `segment()`, inclui a chamada à NPU | 0,88 ms | 12% |
| `cvtColor` BGR→RGB | 0,19 ms | 3% |
| **soma** | **7,28 ms** | |

**Dois terços do custo são decodificar o JPEG da webcam.** A chamada à NPU leva
0,94 ms de parede, das quais 0,78 ms de engine ocupada — a etapa de IA é a mais
barata do pipeline. Trocar MJPG por YUYV eliminaria o decode, mas webcams USB só
oferecem YUYV em resoluções baixas: é limite de banda, não escolha.

### Outros números

Em operação: **29,8 fps**, 33,5 ms por quadro. O gargalo é a câmera a 30 fps, não
o acelerador.

Composição importa mais que o modelo: o feather da máscara em 192×192 com
aritmética `uint8` custa 1,30 ms; o mesmo em 720p com `float32` custa 7,07 ms —
5,4× de diferença. É a flag `--compose fast|float`.

Resolução, pipeline completo com um thread: 640×360 custa 2,24 ms/quadro (7% de um
núcleo), 720p custa 7,23 ms (22%), 1080p custa 16,18 ms (49%). Full HD sustenta
30 fps — a parede de 17,4 ms cabe nos 33 — mas custa 2,24× e **não melhora a borda
do recorte**, porque a máscara continua saindo em 192×192.

## Pré-requisitos

Nada aqui é opcional, e o `install.sh` **aborta** se faltar algo em vez de terminar
dizendo "Feito" numa máquina onde o pipeline é impossível.

### 1. Pacotes do sistema

```bash
sudo apt install dkms build-essential "linux-headers-$(uname -r)" \
                 v4l-utils curl git python3
```

O `v4l-utils` importa mais do que parece: o `blur_cam.py` usa o `v4l2-ctl` para
impedir que a webcam corte a taxa de quadros pela metade com pouca luz.

**Atenção ao pacote de headers.** No Debian, `linux-headers-amd64` resolve para o
kernel do repositório principal, não para o do backports. Use o nome exato que o
`uname -r` devolve, como no comando acima.

### 2. Grupos do usuário

```bash
sudo usermod -aG render,video "$USER"
```

`render` dá acesso a `/dev/accel/accel0` (a NPU) e `video` a `/dev/video*`. **Exige
logout e login para valer** — `groups` só mostra o novo grupo na sessão seguinte. O
instalador do Debian costuma pôr o primeiro usuário em `video`, mas nunca em
`render`; um segundo usuário criado depois não recebe nenhum dos dois.

### 3. Driver de userspace da NPU

Não está nos repositórios do Debian — o `apt install` não encontra nada e não
reclama. Baixe os `.deb` de
[intel/linux-npu-driver](https://github.com/intel/linux-npu-driver/releases) (os
builds `ubuntu24.04` instalam limpo no trixie) e instale com `apt`, não com
`dpkg -i`, para as dependências serem resolvidas:

```bash
sudo apt install libze1
sudo apt install ./intel-level-zero-npu_*.deb \
                 ./intel-driver-compiler-npu_*.deb \
                 ./intel-fw-npu_*.deb
```

Confira:

```bash
ls /dev/accel/accel0          # tem que existir
groups | grep -q render && echo ok
```

A biblioteca chama-se `libze_intel_npu.so`. Procurar por `libze_intel_vpu.so`, como
dizem receitas mais antigas, dá falso negativo e faz parecer que falhou.

### 4. v4l2loopback 0.15.4 ou mais novo

**Não use o pacote do Debian.** A versão empacotada é a 0.15.0, que não compila em
kernel ≥ 7.0 e, se você aplicar patches para compilar, causa page fault no kernel no
primeiro `VIDIOC_QUERYCAP` — travando todo aplicativo que liste webcams. Esse
caminho está descrito em [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) §3
justamente para você **não** segui-lo.

```bash
git clone https://github.com/umlaeute/v4l2loopback
cd v4l2loopback
git checkout v0.15.4
sudo make install-dkms          # compila, assina e registra no DKMS
```

O `install-dkms` cuida do rebuild automático a cada kernel novo.

**Se o Secure Boot estiver ativo**, o módulo precisa de uma chave aceita no
firmware. O DKMS gera e assina sozinho, mas a chave só passa a valer depois de você
confirmar numa tela azul durante o boot:

```bash
sudo mokutil --import /var/lib/dkms/mok.pub    # define uma senha de uso único
sudo reboot                                     # MOK Manager → Enroll MOK → Continue → Yes → senha
sudo mokutil --list-enrolled | grep DKMS        # confirme DEPOIS do reboot
```

Pular essa tela ou errar a senha **apaga o pedido silenciosamente**. Se o `grep` não
achar nada, refaça o `--import` e reinicie.

## Instalação

```bash
git clone https://github.com/giovanibalarini/npu-blur-cam
cd npu-blur-cam
./scripts/fetch-model.sh          # baixa o PPHumanSeg do opencv_zoo e confere o sha256
sudo ./install.sh                 # /opt + nputop + unit + modprobe.d, e carrega o módulo
```

O `install.sh` carrega o módulo ao final e imprime os dois devices criados. Depois,
na sessão de **cada** usuário, sem root:

```bash
systemctl --user daemon-reload
systemctl --user enable --now npu-blur-cam
nputop --once
```

No Meet, Slack ou Teams, escolha **"NPU Blur Cam"** para desfoque ou **"NPU Cam"**
para a imagem limpa.

> **A ordem importa.** Suba o pipeline **antes** de abrir o navegador. Enquanto
> acordado, o serviço segura a webcam em exclusivo; se o Chrome pegá-la primeiro, o
> serviço não consegue abri-la e morre com `nao consegui abrir /dev/video0`. Fechar
> a aba que usa a câmera já resolve — não precisa fechar o navegador.

Se algo falhar, o serviço loga no journal:

```bash
systemctl --user status npu-blur-cam
journalctl --user -u npu-blur-cam -n 40
```

### Rodando do clone, sem instalar em /opt

Ainda precisa dos pré-requisitos 1 a 4 acima — inclusive o driver da NPU, porque o
`--selftest` compila o modelo no acelerador antes de gerar os quadros sintéticos. O
que ele dispensa é apenas a webcam e o v4l2loopback.

```bash
python3 -m venv venv || { python3 -m venv --without-pip venv && \
  curl -fsSL https://bootstrap.pypa.io/get-pip.py | ./venv/bin/python; }
./venv/bin/pip install -r requirements.txt
./scripts/fetch-model.sh
./venv/bin/python blur_cam.py --selftest
```

O `||` acima existe porque o Debian 13 não traz `ensurepip`: o módulo `venv` está
presente, mas o bootstrap do pip não.

## Uso

```
blur_cam.py                      # dois devices, blur na NPU, 1280x720@30
blur_cam.py --no-raw             # so o device com blur, sem o de passagem
blur_cam.py --ov-device GPU      # compara com a iGPU (ou CPU)
blur_cam.py --selftest           # frames sinteticos, sem camera nem loopback
```

Todas as opções:

| Flag | Padrão | O que faz |
|---|---|---|
| `--cam` | `/dev/video0` | webcam de entrada |
| `--out` | `/dev/video9` | device de saída **com** desfoque |
| `--out-raw` | `/dev/video10` | device de passagem, sem desfoque |
| `--no-raw` | — | não publica o device de passagem |
| `--width` / `--height` | `1280` / `720` | resolução de captura e saída |
| `--fps` | `30` | taxa alvo |
| `--ov-device` | `NPU` | onde roda a inferência: `NPU`, `GPU` ou `CPU` |
| `--blur` | `21` | força do desfoque (ímpar) |
| `--smooth` | `0.6` | média móvel da máscara: `0` desliga, `0.9` é muito |
| `--compose` | `fast` | `fast` = feather em 192×192 e uint8; `float` = em 720p e float32 (5,4× mais caro) |
| `--cv-threads` | `1` | threads do OpenCV; `0` = automático. Ver a seção de CPU abaixo |
| `--idle-after` | `5.0` | segundos sem consumidor antes de dormir; `0` desliga |
| `--idle-fps` | `2.0` | taxa mantida nos devices sem consumidor |
| `--selftest` | — | quadros sintéticos, sem câmera nem loopback |

A variável de ambiente `NPU_BLUR_MODEL` sobrescreve o caminho do modelo; sem ela,
ele é procurado em `models/pphumanseg.onnx` ao lado do script.

**Mudar a resolução exige que ninguém esteja consumindo o device** — o v4l2loopback
não deixa alterar o formato com um consumidor aberto, e o produtor continua
silenciosamente no tamanho antigo. Ver TROUBLESHOOTING §7.

### Passando opções para o serviço

O `ExecStart` da unit é fixo. Para mudar:

```bash
systemctl --user edit npu-blur-cam
```

e no arquivo que abrir:

```ini
[Service]
ExecStart=
ExecStart=/opt/npu-blur-cam/venv/bin/python /opt/npu-blur-cam/blur_cam.py --blur 31
```

A linha `ExecStart=` vazia é obrigatória — sem ela o systemd soma os dois comandos
em vez de substituir.

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

## O pool de threads do OpenCV era desperdício

O pipeline custava **51% de um núcleo** antes desta descoberta.

| Threads do OpenCV | CPU/frame | Parede |
|---|---|---|
| 8 (padrão) | 12,84 ms | 8,47 ms |
| 4 | 9,56 ms | 8,39 ms |
| 2 | 8,19 ms | 8,32 ms |
| **1** | **7,28 ms** | **8,47 ms** |

A parede é **idêntica** nos dois extremos. A paralelização gastava 43% mais CPU em
sincronização para entregar exatamente a mesma latência, porque as operações são
pequenas e o orçamento por quadro (33 ms) é folgado. Um thread virou o padrão;
`--cv-threads 0` volta ao automático.

Vale para qualquer pipeline de vídeo em tempo real com folga de latência: **se
sobra parede, threads só custam.**

## Só se calcula o que alguém está olhando

O processo detecta, uma vez por segundo, quem tem cada device aberto:

| Consumidor | O que roda | CPU | NPU |
|---|---|---|---|
| `NPU Blur Cam` | pipeline completo | ~46% de um núcleo | ativa |
| `NPU Cam` | só troca de espaço de cor | ~32% | **parada** |
| Nenhum | dorme, solta a webcam | **~4%** | parada |

O device sem consumidor continua recebendo quadros na taxa mínima — precisa, senão o
`exclusive_caps` o devolve a *Video Output* e ele some da lista de câmeras.

**O device com blur nunca entrega imagem limpa.** Sem consumidor, ele recebe o quadro
inteiro desfocado, não o cru. O pior caso ao trocar para ele é ver tudo borrado por
até um segundo — nunca a sua sala nítida.

## Modo ocioso

Por padrão o serviço **dorme quando ninguém está consumindo** a câmera virtual:
não lê a webcam, não infere, não compõe, e **solta o `/dev/video0`** para outros
aplicativos. Custo em repouso: **2,5% de um núcleo** contra 51% antes.

Ele continua repetindo o último quadro a 2 fps, e isso não é opcional — parar de
transmitir faria o `exclusive_caps` devolver o device a *Video Output*, e ele
sumiria da lista de câmeras do navegador sem disparar `devicechange`.

Acorda em até um segundo quando alguém abre o `/dev/video9`, reabrindo a webcam.
Medido: 150 de 150 quadros entregues a um consumidor que chegou com o serviço
dormindo. `--idle-after 0` desliga o comportamento.

Como efeito colateral, isso **resolve boa parte da limitação de um usuário por
vez**: enquanto ninguém usa o blur, a webcam fica livre para qualquer um.

## Taxa de quadros caindo pela metade no fim do dia

Se o pipeline cair para 15 fps quando escurece, não é ele — é a webcam. Webcams
UVC cortam a taxa pela metade para dobrar o tempo de exposição com pouca luz. O
controle é `exposure_dynamic_framerate`, cujo padrão é 0, mas que aparece ligado:

```bash
v4l2-ctl -d /dev/video0 --set-ctrl=exposure_dynamic_framerate=0
```

Isso mantém o auto-exposure e só proíbe a queda de taxa. O `blur_cam.py` já
aplica sozinho ao abrir a câmera. Medido: 15,0 fps ligado, 29,8 fps desligado,
mesma cena.

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
