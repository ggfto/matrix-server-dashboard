# Solução de problemas

Comece sempre por `./run.sh --detect`: mostra o que o painel enxerga.

## `./install.sh: Permission denied`

Os arquivos perderam a permissão de execução (upload pelo site do GitHub ou zip). Rode
`bash install.sh` uma vez; ele corrige o resto. Ou: `chmod +x *.sh`.

## Containers não aparecem / "docker: not available"

Seu usuário não tem acesso ao Docker:

```bash
docker ps        # se der "permission denied":
sudo usermod -aG docker $USER
```

Saia e entre de novo (ou reinicie o serviço: `sudo systemctl restart matrix-dashboard`).

## Um container aparece sem teste (ENDPOINT `-`)

O teste automático só existe para containers **rodando com porta TCP publicada** (`-p 8080:80`)
ou apps conhecidas em `network_mode: host`. Para os outros, adicione um teste manual em
`checks` (veja [Configuração](CONFIGURACAO.md#monitoramento)).

## Endpoint mostra NO REPLY, mas o app funciona

- O app pode responder só em outro caminho ou exigir HTTPS: crie um teste explícito com a `url`
  certa.
- Se o app usa um domínio no proxy reverso, use `host_header`.

## Um serviço do sistema não aparece

O catálogo só inclui serviços comuns, e só os que estão habilitados ou rodando. Acrescente:

```json
"systemd_units": ["auto", {"name": "MEU SERVIÇO", "unit": "meu-servico"}]
```

## Disco não aparece

Discos menores que 1 GB e montagens de sistema são ignorados. Acrescente:
`"mounts": ["auto", "/caminho"]`.

## Tela preta ou painel não abre no monitor (tty1)

```bash
systemctl status matrix-dashboard
journalctl -u matrix-dashboard -b --no-pager | tail -30
```

- `run.sh` não encontrado: a pasta mudou de lugar; rode `sudo ./install_service.sh` de novo.
- Permissão no `/dev/tty1`: o usuário precisa do grupo `tty` (o `install_service.sh` adiciona;
  vale após reiniciar o serviço).
- Para reiniciar o painel sem sudo: `kill -9 $(systemctl show -p MainPID --value matrix-dashboard)`
  (o systemd sobe de novo em 3 s; um `kill` comum fecha o painel de forma limpa e ele não volta).

## Caracteres estranhos ou quadrados

O painel usa só caracteres presentes na fonte padrão do console Linux. Por SSH, use um terminal
com UTF-8 e uma fonte monoespaçada com caracteres de caixa (DejaVu Sans Mono, JetBrains Mono,
Menlo, Cascadia). Verifique `echo $LANG` (deve terminar em `UTF-8`).

## Cores erradas por SSH

Por SSH o painel usa 256 cores. Se o terminal não suporta, aparecem cores próximas. Use um
terminal moderno ou `TERM=xterm-256color ./run.sh`.

## Painel cortado ou em modo compacto

O layout completo pede 100×30 ou mais (com retrato, 140 colunas). Veja o tamanho com
`stty size`. No monitor, uma resolução maior ou uma fonte menor no console ajuda:
`sudo dpkg-reconfigure console-setup`.

## Temperatura não aparece

O hardware ou a VM não expõe sensores ao Linux. Em máquina física, tente
`sudo apt install lm-sensors && sudo sensors-detect`.

## Teclas D, R ou rede dizem "sem permissão"

Rode `sudo ./install_hotkeys_sudo.sh`. A edição de rede só existe em sistemas com netplan; nos
outros a seção Rede do menu fica só leitura.

## A página web não abre

- `web.enabled` está `true`? A porta está livre (`ss -tlnp | grep 8095`)?
- Seu IP está numa rede de `web.allow`? Fora dela a resposta é `forbidden`.
- Firewall: `sudo ufw allow from 192.168.1.0/24 to any port 8095`.

## Uso de CPU alto

Baixe `fps` (6 é leve), use a tecla `E` (economia) ou desligue a chuva (`M`). O espelho web só
renderiza quando há alguém conectado.
