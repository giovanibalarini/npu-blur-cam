#!/bin/bash
# Instala o pipeline em /opt/npu-blur-cam, compartilhado entre todos os
# usuarios da maquina. Rodar como root, a partir do clone do repo.
#
# NAO instala o v4l2loopback nem o driver da NPU — veja o README, esses
# dois tem pre-requisitos (headers do kernel exato, MOK do Secure Boot)
# que nao cabem num script nao-interativo.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "rode como root" >&2; exit 1; }

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST=/opt/npu-blur-cam

echo "==> 1/6 checando pre-requisitos"
[ -e /dev/accel/accel0 ] || echo "  AVISO: /dev/accel/accel0 ausente — o intel_vpu esta carregado?"
modinfo v4l2loopback >/dev/null 2>&1 || echo "  AVISO: v4l2loopback nao instalado — veja o README"
command -v python3 >/dev/null || { echo "  python3 ausente" >&2; exit 1; }

echo "==> 2/6 venv em $DST/venv"
mkdir -p "$DST"
python3 -m venv "$DST/venv" 2>/dev/null || {
  echo "  python3-venv ausente; tentando bootstrap com get-pip"
  python3 -m venv --without-pip "$DST/venv"
  curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$DST/venv/bin/python"
}
"$DST/venv/bin/pip" install --quiet --upgrade pip
"$DST/venv/bin/pip" install --quiet -r "$SRC/requirements.txt"

echo "==> 3/6 codigo e modelo"
install -m 0755 "$SRC/blur_cam.py" "$DST/blur_cam.py"
mkdir -p "$DST/models"
if [ -f "$SRC/models/pphumanseg.onnx" ]; then
  install -m 0644 "$SRC/models/pphumanseg.onnx" "$DST/models/pphumanseg.onnx"
else
  echo "  modelo ausente — rode scripts/fetch-model.sh antes" >&2; exit 1
fi

echo "==> 4/6 nputop em /usr/local/bin"
install -m 0755 "$SRC/nputop" /usr/local/bin/nputop

echo "==> 5/6 configuracao do v4l2loopback"
install -m 0644 "$SRC/etc/modprobe.d/v4l2loopback.conf"   /etc/modprobe.d/v4l2loopback.conf
install -m 0644 "$SRC/etc/modules-load.d/v4l2loopback.conf" /etc/modules-load.d/v4l2loopback.conf
echo "  para aplicar agora: rmmod v4l2loopback && modprobe v4l2loopback"

echo "==> 6/6 unit de usuario compartilhada"
install -m 0644 "$SRC/systemd/npu-blur-cam.service" /etc/systemd/user/npu-blur-cam.service

chown -R root:root "$DST"; chmod -R a+rX "$DST"

echo
"$DST/venv/bin/python" -c "import openvino as ov; print('  devices:', ov.Core().available_devices)" || true
cat <<'MSG'

Feito. Agora, na sessao de CADA usuario que for usar (sem root):

  systemctl --user daemon-reload
  systemctl --user enable --now npu-blur-cam
  nputop

Lembre: um usuario por vez. O servico segura a camera fisica em exclusivo.
MSG
