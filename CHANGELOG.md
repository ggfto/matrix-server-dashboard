# Changelog

## Não lançado

- Código dividido no pacote `matrixdash` (o `matrix_dashboard.py` virou só o ponto de entrada),
  com três interfaces: `ContainerRuntime` (Docker hoje), `Integration` (Claude Code, backup,
  iCloud, Tailscale) e `Panel` (cada bloco da tela). Detalhes em `docs/DESENVOLVIMENTO.md`.
- Testes: telas de referência, leitura do Docker, teclas e ações, e um teste do programa real.
- k3s: workloads do cluster no painel de serviços (prontos/desejados, CPU, memória, erros dos
  pods como alerta) e "reiniciar" pelo menu, com uma ServiceAccount própria
  (`system/k8s/make-kubeconfig.sh`).
- Novas chaves `runtimes` (padrão `["auto"]`: Docker e k3s, os que existirem) e `kubeconfig`.
- O letreiro do cabeçalho mostra os limites do Claude e o progresso do iCloud mesmo sem backup
  rodando (antes só apareciam junto do backup).

## 3.0 — 2026-11-15

Primeira versão pública.

- Detecção automática de discos, interface, serviços do host (~35), testes de endpoint por
  container (atualizados ao vivo) e redes liberadas para a página web: marcador `"auto"` no
  `config.json`, com itens extras mantidos.
- Instalador interativo (`install.sh`): pergunta só nome, título, foto e acesso web.
- `install_service.sh` (painel no tty1), `uninstall.sh`, opção `--detect`.
- Sem foto, o retrato mostra o nome do servidor em letras grandes.
- Claude Code, backup e iCloud passam a ser opcionais: sem configuração, somem da tela, da barra
  de teclas e do menu.
- Espelho web: controle e terminal desligados por padrão; terminal virou opção própria
  (`web.terminal`).
- Helper de rede detecta a interface e o arquivo netplan; regra do sudo para qualquer usuário.
- Documentação completa em `docs/` e prints gerados por `tools/screenshots.py`.

## 2.x — 2026-10

Versão de uso pessoal: boot animado, retrato com glitch, chuva, console de eventos, mapa da
rede, menu de configurações, espelho web e terminal.
