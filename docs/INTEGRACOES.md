# Integrações

Todas são opcionais. Quando não estão configuradas, as partes da tela, as teclas e as seções do
menu correspondentes não aparecem.

## Claude Code

Detectado automaticamente (`"claude": "auto"`) quando o executável `claude` existe.

O painel mostra:

- **Processos**: online/offline, número de sessões, remote-control (nome), versão, CPU,
  memória, há quanto tempo está aberto e se está trabalhando (CPU acima de 4%) ou ocioso, com
  histórico.
- **Uso** (lido de `~/.claude/projects`): tokens e custo estimado de hoje, da semana e das
  últimas 5 h, gráficos por hora e por dia e totais por modelo. O retrato alterna com esse
  relatório; a tecla `U` fixa um dos dois.
- **Limites do plano** (opcional): o Claude Code não expõe os limites por arquivo. Se você usa
  um script de statusline que salva o JSON da statusline, aponte `claude_limits_file` para ele
  (padrão `~/.claude/statusline-last.json`). O painel lê `rate_limits.five_hour` e
  `rate_limits.seven_day` (`used_percentage` e `resets_at`). `claude_plan` é só um rótulo
  (ex.: `"Pro"`).

```json
"claude": "auto",
"claude_plan": "Pro",
"claude_limits_file": "~/.claude/statusline-last.json"
```

Use `"claude": false` para esconder tudo mesmo com o Claude Code instalado.

## Status de um backup externo

O painel pode mostrar o progresso de qualquer script de backup (rclone, restic, rsync…) que
grave um arquivo JSON de estado. Aponte `backup_state` para ele:

```json
"backup_state": "~/backup/state.json"
```

O script deve reescrever o arquivo a cada poucos segundos enquanto roda (se ficar mais de 90 s
sem atualizar durante `running`, o painel mostra `stalled`). Campos lidos:

| Campo | Exemplo | Uso |
|---|---|---|
| `status` | `running`, `idle`, `done`, `paused`, `error`, `not_started` | estado e cor |
| `phase` | `scanning`, `comparing`, `uploading` | fase atual |
| `updated` | `1731672000` | horário (epoch) da última escrita |
| `dest` | `"gdrive:Backup"` | destino exibido |
| `source_bytes` / `uploaded_bytes` | números | progresso da cópia inicial |
| `initial_complete` | `true` | cópia inicial terminada |
| `today` / `run` | objetos com bytes e arquivos | totais do dia e da execução atual |

Com `backup_state` definido, a pasta do arquivo vira a pasta de controle e aparecem as teclas:

| Tecla | Arquivo criado/apagado na pasta | O script deve… |
|---|---|---|
| `B` | `PAUSE` (cria/apaga) | não iniciar enquanto `PAUSE` existir |
| `S` | `RUN_NOW` (cria) | rodar já e apagar o arquivo |
| `V` | `TURBO` (cria/apaga) | ignorar o limite de banda enquanto existir |

Se a pasta tiver um `backup.json` com `bwlimit` (ex.: `"08:00,3M 23:00,8M"`) e `daily_sync_at`
(ex.: `"02:00"`), o menu `C` ganha a seção de backup para editar esses valores.

## iCloud (icloudpd)

Para quem baixa fotos do iCloud com o container
[icloudpd](https://github.com/boredazfcuk/docker-icloudpd):

```json
"icloud": {"container": "icloudpd", "folder": "/srv/fotos/icloud", "limit": "off"}
```

- O painel lê os logs do container e o tamanho da pasta para mostrar status, itens baixados,
  velocidade e total do dia. Avisa quando a Apple pede login de novo.
- Tecla `P` pausa/retoma o container.
- `limit` (`off`, `1M`, `2M`, `3M`, `5M`) limita a banda de download dentro do container. Para
  isso, crie uma vez a imagem auxiliar:
  ```bash
  docker build -t matrix-tc system/tc
  ```
