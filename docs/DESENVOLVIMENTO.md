# Desenvolvimento

## Estrutura

```
matrix_dashboard.py    painel: coleta (Collector), desenho (Canvas/Screen), retrato, chuva,
                       menu de configurações (SettingsMenu) e laço principal (Dashboard)
autodetect.py          detecção de discos, interface, serviços, testes de containers e redes
netmap.py              mapa da rede: vizinhos ARP/IPv6, mDNS/NetBIOS, Tailscale, redes Docker
web_view.py            servidor HTTP do espelho (Server-Sent Events) + terminal web opcional
claude_usage.py        leitura do uso de tokens do Claude Code (~/.claude/projects)
web/index.html         página do espelho (xterm.js em web/static/)
system/matrix-netcfg   helper root que aplica IP fixo/DHCP via netplan, com rollback
system/tc/Dockerfile   imagem mínima com `tc`, usada para limitar a banda do icloudpd
install.sh             instalador interativo
install_service.sh     serviço systemd no tty1
install_hotkeys_sudo.sh regra do sudo para D/R/rede
uninstall.sh           remove o que foi instalado fora da pasta
tools/screenshots.py   gera os prints da documentação com dados de demonstração
config.example.json    modelo de configuração
```

## Como funciona

- **Coleta**: duas threads. A rápida (1 s) lê CPU, memória, rede, `docker ps` e processos do
  Claude e monta um `Snapshot` novo. A lenta lê `docker stats`, `systemctl is-active`, Tailscale e
  faz os testes HTTP/TCP. O desenho só lê `collector.snap`, trocado de forma atômica.
- **Desenho**: cada quadro é desenhado num `Canvas` (caracteres + estilos); o `Screen` compara
  com o quadro anterior e envia ao terminal só o que mudou. O espelho web usa o mesmo canvas.
- **Detecção**: `merge_config()` junta o `config.json` com os padrões e chama
  `autodetect.resolve()`, que troca os marcadores `"auto"`. Os testes de containers são
  recalculados a cada ciclo lento por `autodetect.live_checks()`.

## Rodar em desenvolvimento

```bash
./install.sh --yes
.venv/bin/python matrix_dashboard.py --config config.json            # painel
.venv/bin/python matrix_dashboard.py --config config.json --once     # um quadro
.venv/bin/python matrix_dashboard.py --config config.json --once --at 2.0   # quadro do boot
.venv/bin/python autodetect.py                                       # só a detecção
COLUMNS=90 LINES=26 .venv/bin/python matrix_dashboard.py --once     # testar outro tamanho
```

## Gerar os prints da documentação

```bash
.venv/bin/python tools/screenshots.py            # grava em docs/screenshots/
```

O script desenha o painel com o renderizador real e dados fixos de demonstração (hostname
`matrix-lab`, IPs de exemplo, containers fictícios), então nada do servidor onde ele roda vai
para as imagens. Precisa da fonte DejaVu Sans Mono (`sudo apt install fonts-dejavu-core`).

## Adicionar um serviço ao catálogo

Em `autodetect.py`, acrescente em `SERVICE_CATALOG` o rótulo e as units candidatas (a primeira
instalada vence; `*` aceita instâncias, ex.: `syncthing@*.service`):

```python
("MEU SERVIÇO", ["meu-servico.service", "meu-servico-alt.service"]),
```

## Adicionar uma app com teste HTTP

Em `KNOWN_IMAGES`: trecho do nome da imagem, porta **interna** do container, caminho de saúde
(ou `None` para TCP) e rótulo. A ordem importa: o primeiro trecho que casar vence.

```python
("minha-app", 8080, "/healthz", "MINHA APP"),
```

## Convenções

- Interface em português; código, comentários e nomes em inglês.
- Só caracteres presentes na fonte do console Linux (Uni2-Fixed16) na tela.
- Recursos opcionais não podem aparecer quebrados: sem configuração, ficam escondidos.
- Sem dependências além de `psutil` e `Pillow`.
