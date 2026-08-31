#!/bin/bash
# Baixa o PPHumanSeg do opencv_zoo. O modelo nao e versionado neste repo
# porque nao e nosso — a fonte e https://github.com/opencv/opencv_zoo,
# em models/human_segmentation_pphumanseg/.
set -euo pipefail

DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/models"
OUT="$DEST/pphumanseg.onnx"
SHA256="552d8a984054e59b5d773d24b9b12022b22046ceb2bbc4c9aaeaceb36a9ddf24"
URL="https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/human_segmentation_pphumanseg/human_segmentation_pphumanseg_2023mar.onnx"

mkdir -p "$DEST"
if [ -f "$OUT" ] && echo "$SHA256  $OUT" | sha256sum -c --status; then
  echo "modelo ja presente e integro: $OUT"; exit 0
fi

echo "baixando de $URL"
curl -fSL --retry 3 -o "$OUT.tmp" "$URL"

if ! echo "$SHA256  $OUT.tmp" | sha256sum -c --status; then
  echo "ERRO: checksum diferente do validado." >&2
  echo "  esperado: $SHA256" >&2
  echo "  obtido:   $(sha256sum "$OUT.tmp" | cut -d' ' -f1)" >&2
  echo "O upstream aponta para a branch main e pode ter republicado o modelo." >&2
  echo "Confira antes de usar; para aceitar mesmo assim: $0 --force" >&2
  if [ "${1:-}" != "--force" ]; then rm -f "$OUT.tmp"; exit 1; fi
  echo "--force: seguindo com o arquivo divergente." >&2
fi

mv "$OUT.tmp" "$OUT"
echo "ok: $OUT ($(stat -c%s "$OUT") bytes)"
