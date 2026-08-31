#!/bin/bash
# Instala o pipeline em /opt/npu-blur-cam, compartilhado entre todos os usuarios
# da maquina. Rodar como root, a partir do clone do repositorio.
#
# NAO instala o driver de userspace da NPU nem o v4l2loopback — os dois tem
# passos interativos (chave MOK do Secure Boot) ou dependem da versao exata do
# seu kernel. O README explica os dois, e este script ABORTA se faltarem, em
# vez de terminar com "Feito." numa maquina onde o pipeline e impossivel.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "rode como root" >&2; exit 1; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST=/opt/npu-blur-cam
FALTOU=0

erro() { echo "  FALTA: $*" >&2; FALTOU=1; }

echo "==> 1/7 pre-requisitos"

command -v python3 >/dev/null || erro "python3"
command -v curl    >/dev/null || erro "curl"
command -v v4l2-ctl >/dev/null || erro "v4l2-ctl — apt install v4l-utils"

if [ ! -e /dev/accel/accel0 ]; then
  erro "/dev/accel/accel0 — driver da NPU nao instalado. Veja o README, secao Pre-requisitos."
fi

if ! modinfo v4l2loopback >/dev/null 2>&1; then
  erro "modulo v4l2loopback. O do Debian (0.15.0) NAO serve — veja o README."
else
  V=$(modinfo -F version v4l2loopback 2>/dev/null || echo "?")
  case "$V" in
    0.15.0|0.14*|0.13*|0.12*)
      erro "v4l2loopback $V derruba o kernel em >= 7.0. Precisa da 0.15.4 — veja o README." ;;
  esac
fi

if [ ! -f "$SRC/models/pphumanseg.onnx" ]; then
  erro "modelo. Rode: ./scripts/fetch-model.sh"
fi

[ "$FALTOU" -eq 0 ] || { echo; echo "abortando: resolva o acima e rode de novo." >&2; exit 1; }
echo "  tudo presente"

echo "==> 2/7 venv em $DST/venv"
mkdir -p "$DST"
if ! python3 -m venv "$DST/venv"; then
  # Debian 13 nao traz ensurepip; o modulo venv existe, so o bootstrap do pip falta.
  echo "  venv sem pip (ensurepip ausente) — bootstrapping com get-pip.py"
  python3 -m venv --without-pip "$DST/venv"
  curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$DST/venv/bin/python"
fi
"$DST/venv/bin/pip" install --quiet --upgrade pip
"$DST/venv/bin/pip" install --quiet -r "$SRC/requirements.txt"

echo "==> 3/7 codigo e modelo"
install -m 0755 "$SRC/blur_cam.py" "$DST/blur_cam.py"
mkdir -p "$DST/models"
install -m 0644 "$SRC/models/pphumanseg.onnx" "$DST/models/pphumanseg.onnx"
chown -R root:root "$DST"; chmod -R a+rX "$DST"

echo "==> 4/7 nputop em /usr/local/bin"
install -m 0755 "$SRC/nputop" /usr/local/bin/nputop

echo "==> 5/7 configuracao do v4l2loopback"
install -m 0644 "$SRC/etc/modprobe.d/v4l2loopback.conf"     /etc/modprobe.d/v4l2loopback.conf
install -m 0644 "$SRC/etc/modules-load.d/v4l2loopback.conf" /etc/modules-load.d/v4l2loopback.conf

echo "==> 6/7 carregando o modulo com os parametros novos"
# 'rmmod && modprobe' falha na primeira instalacao: sem o modulo carregado o
# rmmod retorna != 0 e o && curto-circuita.
if lsmod | grep -q '^v4l2loopback'; then
  if ! modprobe -r v4l2loopback 2>/dev/null; then
    echo "  modulo em uso — nao consegui recarregar. Feche o navegador e rode:" >&2
    echo "    pkill -f 'utility-sub-type=video[_]capture'; modprobe -r v4l2loopback && modprobe v4l2loopback" >&2
  fi
fi
modprobe v4l2loopback
for d in /dev/video9 /dev/video10; do
  [ -e "$d" ] && echo "  $d: $(v4l2-ctl -d $d --info 2>/dev/null | sed -n 's/.*Card type *: //p')" \
              || echo "  AVISO: $d nao apareceu"
done

echo "==> 7/7 unit de usuario compartilhada"
install -m 0644 "$SRC/systemd/npu-blur-cam.service" /etc/systemd/user/npu-blur-cam.service

echo
echo "==> verificando que a NPU e alcancavel"
DEVS=$("$DST/venv/bin/python" -c "import openvino as ov; print(','.join(ov.Core().available_devices))" 2>&1) || {
  echo "  ERRO: o OpenVINO nao carregou: $DEVS" >&2; exit 1; }
echo "  available_devices: $DEVS"
case "$DEVS" in
  *NPU*) echo "  NPU presente." ;;
  *) echo >&2
     echo "  ERRO: NPU ausente. O driver de userspace nao esta completo, ou o" >&2
     echo "  usuario que vai rodar nao esta no grupo 'render'. Veja o README." >&2
     exit 1 ;;
esac

cat <<'MSG'

Feito. Agora, na sessao de CADA usuario que for usar (sem root):

  sudo usermod -aG render,video "$USER"   # se ainda nao estiver; exige logout/login
  systemctl --user daemon-reload
  systemctl --user enable --now npu-blur-cam
  nputop --once

No Meet/Slack/Teams, escolha "NPU Blur Cam" para desfoque ou "NPU Cam" para a
imagem limpa. Trocar entre as duas e o liga-desliga.

ATENCAO: suba o pipeline ANTES de abrir o navegador. Se o Chrome pegar a webcam
primeiro, o servico nao consegue abri-la.

Reinstalando por cima? Rode tambem:
  systemctl --user restart npu-blur-cam
MSG
