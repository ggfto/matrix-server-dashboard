# Instalação

## Requisitos

| Item | Observação |
|---|---|
| Linux com systemd | testado em Ubuntu Server 22.04 e 24.04; Debian e derivados devem funcionar |
| Python 3.10 ou mais novo, com `venv` | no Ubuntu: `sudo apt install python3 python3-venv` (o instalador oferece fazer isso) |
| Terminal com 100×30 ou mais | menor que isso o painel entra no modo compacto |
| Opcional: Docker | para ver containers; seu usuário precisa estar no grupo `docker` |
| Opcional: Tailscale, Claude Code | aparecem no painel só se estiverem instalados |
| Opcional: netplan | só para mudar IP fixo/DHCP pelo menu (padrão no Ubuntu Server) |

O painel usa ~40 MB de RAM e 1–3% de CPU a 10 quadros por segundo. Com a tecla `E` (economia)
cai para 4 quadros, sem a chuva.

## 1. Baixar

```bash
git clone https://github.com/tiagomoreiraweb/matrix-server-dashboard.git
cd matrix-server-dashboard
```

Ou baixe o `.zip` da página do projeto e extraia:

```bash
unzip matrix-server-dashboard.zip && cd matrix-server-dashboard
```

A pasta pode ficar em qualquer lugar (ex.: `~/matrix-server-dashboard`). O serviço do monitor
aponta para ela, então se mover a pasta depois rode `sudo ./install_service.sh` de novo.

## 2. Rodar o instalador

```bash
./install.sh
```

Se aparecer `Permission denied` (acontece quando os arquivos foram enviados pelo site do GitHub
ou extraídos de um zip sem permissões), rode `bash install.sh`: o instalador corrige as
permissões dos outros scripts sozinho.

Ele faz quatro etapas:

1. **Ambiente Python**: cria `.venv` dentro da pasta e instala `psutil` e `Pillow`. Nada vai
   para o sistema.
2. **Configuração básica**: quatro perguntas.
   - **Seu nome**: aparece no boot e no retrato.
   - **Título**: vazio usa `<NOME-DO-SERVIDOR> // MATRIX SERVER MONITOR`.
   - **Foto**: caminho de um `.jpg`/`.png`, convertido em ASCII; vazio mostra o nome do servidor
     em letras grandes. Fotos de rosto com fundo simples e bom contraste ficam melhores.
   - **Painel no navegador**: se sim, pergunta a porta e se a página pode controlar o painel e
     abrir terminal (os dois vêm desligados por segurança).
3. **Detecção**: mostra o que foi encontrado, por exemplo:
   ```
   host        meu-servidor
   interface   eth0
   disks       /, /mnt/storage
   services    DOCKER ENGINE (docker), SSH (ssh), TAILSCALE (tailscaled), NGINX (nginx)
   docker      6 containers
     probe     IMMICH             http://127.0.0.1:2283/api/server/ping
     probe     JELLYFIN           http://127.0.0.1:8096/health
   tailscale   yes
   claude code no
   web mirror  port 8095  allow 127.0.0.1/32, ::1/128, 100.64.0.0/10, 192.168.1.0/24
   ```
4. **Opcionais com sudo**: abrir o painel no monitor ao ligar e liberar as teclas de
   desligar/reiniciar/rede. Dá para fazer depois (veja abaixo).

Instalação sem perguntas, com os padrões: `./install.sh --yes`. Nesse modo os passos com sudo
não rodam; o instalador só mostra os comandos.

Para refazer as perguntas, apague o `config.json` e rode `./install.sh` de novo.

## 3. Testar

```bash
./run.sh            # painel no terminal atual; Q fecha (pede confirmação), Ctrl+C sai direto
./run.sh --once     # imprime um quadro e sai (útil por SSH)
./run.sh --detect   # mostra a detecção de novo
```

## 4. Painel no monitor do servidor ao ligar (opcional)

```bash
sudo ./install_service.sh
```

Cria o serviço `matrix-dashboard` que ocupa o primeiro console (tty1) no lugar do prompt de
login. Para fazer login no próprio monitor, use `Alt+F2` (outro console) ou a tecla `X` do painel
(abre um terminal e volta ao painel com `exit`).

```bash
systemctl status matrix-dashboard     # estado
sudo systemctl restart matrix-dashboard
sudo systemctl disable --now matrix-dashboard   # parar de abrir ao ligar
```

O script também coloca o usuário nos grupos `tty` (desenhar no console) e `docker` (ler os
containers), se existirem.

## 5. Teclas de desligar, reiniciar e rede (opcional)

```bash
sudo ./install_hotkeys_sudo.sh
```

Cria `/etc/sudoers.d/matrix-dashboard` permitindo ao usuário do painel, sem senha, **apenas**:

- `systemctl poweroff` (tecla `D`) e `systemctl reboot` (tecla `R`);
- `/usr/local/sbin/matrix-netcfg`, que aplica IP fixo ou DHCP pelo menu `C`. Ele só é instalado
  em sistemas com netplan, valida os valores e restaura a configuração anterior se o roteador
  não responder em 30 segundos.

Sem esse passo o painel funciona igual; só essas três ações ficam bloqueadas.

## Atualizar

```bash
cd matrix-server-dashboard
git pull
./install.sh                          # atualiza dependências; mantém seu config.json
sudo systemctl restart matrix-dashboard   # se usa o serviço
```

Seu `config.json`, sua foto (`assets/portrait.*`) e o histórico do mapa de rede não são
versionados, então o `git pull` não mexe neles.

## Desinstalar

```bash
sudo ./uninstall.sh     # remove o serviço, a regra do sudo e o helper de rede
cd .. && rm -rf matrix-server-dashboard
```

Se o tty1 ficar sem prompt de login depois disso: `sudo systemctl enable --now getty@tty1`.
