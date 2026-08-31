# Armadilhas

Em ordem: cada uma só apareceu depois que a anterior saiu do caminho. Nenhuma
delas é problema de machine learning.

## 1. O v4l2loopback empacotado não compila em kernel ≥ 7.0

**Sintoma:** três erros de build no DKMS.

Duas mudanças de API do kernel. `v4l2_fh_add()` e `v4l2_fh_del()` passaram a
receber um `struct file *`, e o guard do timer
(`#if defined(timer_setup) && defined(from_timer)`) falha porque o 6.16 renomeou
`from_timer()` para `timer_container_of()` — jogando o módulo num ramo legado
de kernel anterior ao 4.15.

**Solução:** v4l2loopback **0.15.4** do upstream. Ele resolve os dois com guards
em `KERNEL_VERSION(6, 18, 0)`; nenhum patch local é necessário.

Cuidado no Debian: `linux-headers-amd64` resolve para o kernel do repositório
principal, não para o do backports. Instale o nome exato:

```bash
apt install linux-headers-$(uname -r)
```

## 2. Secure Boot descarta o pedido de chave sem dizer nada

**Sintoma:** `modprobe` rejeitado, e nada no log explicando.

Módulo fora da árvore precisa da chave MOK aceita no firmware. O DKMS gera
`/var/lib/dkms/mok.{key,pub}` e assina sozinho, mas a chave só passa a valer
depois da tela azul do MOK Manager no boot.

```bash
mokutil --import /var/lib/dkms/mok.pub   # define senha de uso unico
reboot                                   # -> Enroll MOK -> Continue -> Yes -> senha
mokutil --list-enrolled | grep DKMS      # confirme DEPOIS do reboot
```

**Pular a tela azul, ou errar a senha, apaga o pedido silenciosamente.** O shim
limpa a variável `MokNew` tanto ao enrolar quanto ao ser dispensado — as duas
situações são indistinguíveis depois. Se `--list-enrolled` não mostrar a chave,
refaça o `--import` e reinicie.

Para conferir que o módulo está assinado com a chave certa: o `sig_key` do
`modinfo` é o **serial** do certificado, não o Subject Key Identifier.

```bash
modinfo v4l2loopback | grep sig_key
openssl x509 -in /var/lib/dkms/mok.pub -inform DER -noout -serial
```

## 3. O módulo carrega e derruba o kernel

**Sintoma:** todo aplicativo que lista webcam vira processo zumbi.

Isto é específico da 0.15.0 com patches caseiros para compilar. Qualquer
`VIDIOC_QUERYCAP` em `/dev/video9` causa page fault:

```
BUG: unable to handle page fault for address: fffffffffffffffc
RIP: vidioc_querycap+0xa1 [v4l2loopback]
```

O core passa `fh = NULL` e o módulo faz aritmética de ponteiro em cima disso.

O estrago é automático e silencioso: o `v4l_id` do udev chama QUERYCAP sozinho
durante o `modprobe`, então o primeiro oops acontece antes de qualquer teste
manual. Slack, Chrome, Cheese e o próprio `v4l2-ctl` morrem ao enumerar câmeras.

**Se você chegou aqui:** não abra nada que liste câmeras, remova o
`/etc/modules-load.d/v4l2loopback.conf` para não repetir no boot, e reinicie —
o `rmmod` provavelmente falha porque as tasks mortas seguram o refcount. Depois
instale a 0.15.4.

## 4. O Chrome diz "câmera indisponível" com o device perfeito

**Sintoma:** nenhum erro em lugar nenhum. O produtor entrega 30 fps e um
consumidor OpenCV lê frames do mesmo `/dev/video9` ao vivo.

**Causa:** o v4l2loopback entrega `max_buffers=2` por padrão. O Chrome pede
quatro no `REQBUFS`, aceita os dois, mapeia — e depois passa fome no `DQBUF`.

```bash
# o sinal que separa "device quebrado" de "Chrome quebrado":
fuser -v /dev/video9
```

Se o Chrome aparece com `F...m` (aberto **e** mapeado em memória), ele passou
por `open`, `S_FMT` e `REQBUFS` — a falha é de streaming, não de enumeração nem
de formato. E se um consumidor independente lê frames em paralelo, o device
está são.

**Correção:** `max_buffers=8` no `/etc/modprobe.d/v4l2loopback.conf`. Editar não
basta, o módulo precisa recarregar:

```bash
systemctl --user stop npu-blur-cam
rmmod v4l2loopback && modprobe v4l2loopback
cat /sys/module/v4l2loopback/parameters/max_buffers   # tem que dizer 8
```

### Corolário: o cache do VideoCaptureService

O `VideoCaptureService` do Chrome cacheia o descritor do device e **sobrevive ao
fechamento das abas**. Depois de qualquer `rmmod`/`modprobe` ele fica com o
device velho em cache e continua falhando. Não precisa fechar o navegador —
mate só essa peça, que o Chrome respawna sem perder aba nenhuma:

```bash
pkill -f video_capture.mojom
```

Sinal de que ainda está ruim: o PID que segura o `/dev/video9` muda a cada
poucos segundos. Quando conserta, o PID fica fixo e a posse vira contínua.

## 5. A câmera virtual sumiu da lista do Meet

**Causa:** com `exclusive_caps=1`, sem produtor escrevendo o device volta a
anunciar `Video Output` — deixa de ser câmera. O navegador o tira da lista, e
nenhum evento `devicechange` dispara no JS, porque o nó nunca desapareceu: só
mudou de capacidade.

Num episódio real bastou um intervalo de cerca de três segundos sem produtor,
ao trocar o processo manual pelo serviço.

**Correção:** recarregue a aba (F5). **Prevenção:** deixe o serviço no
autostart e nunca derrube o produtor com o navegador segurando a câmera.

## Diagnóstico rápido

```bash
lsmod | grep v4l2loopback                            # modulo carregado?
cat /sys/module/v4l2loopback/parameters/max_buffers   # 8?
v4l2-ctl -d /dev/video9 --all | grep -A4 "Device Caps"  # Video Capture com produtor ativo?
fuser -v /dev/video0 /dev/video9                      # quem segura o que
nputop --once                                         # a NPU esta trabalhando?
systemctl --user status npu-blur-cam
journalctl --user -u npu-blur-cam -n 30
```

## 6. Os rótulos das câmeras aparecem com aspas

Sintoma: o Meet lista `NPU Blur Cam"` e `"NPU Cam`.

O `card_label` do v4l2loopback leva **uma** string com vírgulas dentro, não uma lista
de strings entre aspas. Escrever `card_label="A","B"` coloca as aspas literais nos
rótulos.

```
# errado
options v4l2loopback devices=2 card_label="NPU Blur Cam","NPU Cam"
# certo
options v4l2loopback devices=2 card_label="NPU Blur Cam,NPU Cam"
```

Corrigir exige recarregar o módulo — e o módulo só descarrega se ninguém tiver os
devices abertos:

```bash
pkill -f "utility-sub-type=video[_]capture"   # o colchete evita o pkill se auto-matar
systemctl --user stop npu-blur-cam
sudo rmmod v4l2loopback && sudo modprobe v4l2loopback
systemctl --user start npu-blur-cam
```

## 7. Mudar resolução não faz efeito

O v4l2loopback **não deixa alterar o formato enquanto houver consumidor**. Trocar
`--width/--height` e reiniciar o serviço não muda nada se o navegador estiver com o
device aberto — o produtor pede o tamanho novo e continua silenciosamente no antigo.

Sintoma: `v4l2-ctl --get-fmt-video` segue mostrando a resolução velha depois do
restart. Mesma solução do item 6: tirar o consumidor primeiro.
