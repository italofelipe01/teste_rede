# Monitor de Estabilidade de Rede

Monitora a conexão desta máquina até um destino (padrão `8.8.8.8`), salto a salto, e mostra tudo em um
painel web em tempo real: disponibilidade, quedas (com o ponto da rota onde a falha ocorreu), perda,
latência, jitter e diagnóstico automático. Gera evidências (CSV `;`, resumo, relatório e pacote `.zip`)
prontas para enviar ao suporte técnico ou ao provedor.

- **Windows, Linux e macOS**, sem privilégios de administrador.
- **Zero dependências**: só Python 3.9+ e o `ping` do sistema. Nada de `pip install`.
- **Roda localmente**: os dados ficam nesta máquina; o painel funciona mesmo com a internet fora do ar.
- **Sem intervenção manual**: começa a monitorar sozinho, tenta de novo quando a rede/DNS falham,
  redescobre a rota periodicamente e arquiva cada sessão automaticamente.

## Início rápido

| Sistema | Como iniciar |
|---|---|
| Windows | Dê **duplo clique em `iniciar.bat`**. Se o Python não estiver instalado, o script oferece instalar via `winget`. |
| macOS | Dê **duplo clique em `iniciar.command`** (ou rode `./iniciar.sh` no Terminal). |
| Linux | Rode **`./iniciar.sh`**. Se faltar Python ou `ping`, o script oferece instalar pelo gerenciador de pacotes. |

O painel abre sozinho no navegador em **http://127.0.0.1:8000**. Se a porta estiver ocupada, a próxima
livre é usada (o endereço aparece no terminal). Rodar o script de novo com o monitor já aberto apenas
abre o painel existente, sem iniciar um segundo monitor.

Para encerrar, feche a janela ou pressione `Ctrl+C` (as evidências são finalizadas ao sair).

Verificar o ambiente sem iniciar o monitor:

```sh
python portal_rede.py --check        # Windows: iniciar.bat --check
```

## Acessar o painel por outros dispositivos da rede

Por padrão o painel só aceita conexões desta máquina. Para abrir no celular ou em outro computador da
mesma rede:

```sh
./iniciar.sh --lan                   # Windows: iniciar.bat --lan
```

ou, de forma permanente, `"host": "0.0.0.0"` no `config.json`. Os endereços de acesso (ex.:
`http://192.168.0.10:8000`) aparecem no terminal e no painel. No Windows, o firewall pode pedir permissão
na primeira vez.

Dispositivos da rede **visualizam** o painel; alterar configurações e iniciar sessões continua restrito à
máquina local (libere com `"allow_remote_control": true` se precisar).

## Configuração

Não é obrigatório configurar nada. Quando necessário, use o botão **Configurações** do painel: as
alterações valem na hora e ficam salvas em `config.json`. Também é possível editar o arquivo (modelo em
[`config.example.json`](config.example.json)) ou passar argumentos na linha de comando, que têm prioridade.

| Chave em `config.json` | Argumento | Padrão | Descrição |
|---|---|---|---|
| `target` | `destino` (posicional) | `8.8.8.8` | IP ou host monitorado |
| `interval_sec` | `--intervalo` | `1.0` | Segundos entre ciclos de medição |
| `timeout_ms` | `--timeout-ms` | `1000` | Tempo máximo de espera de cada ping |
| `max_hops` | `--max-hops` | `30` | Máximo de saltos na descoberta da rota |
| `outage_min_cycles` | `--min-ciclos-queda` | `3` | Ciclos seguidos sem resposta do destino para confirmar uma queda |
| `route_refresh_min` | `--redescobrir-min` | `10` | Minutos entre redescobertas da rota (`0` desliga) |
| `host` | `--host` / `--lan` | `127.0.0.1` | Interface do painel (`0.0.0.0` = rede local) |
| `port` | `--port` | `8000` | Porta do painel |
| `open_browser` | `--no-browser` | `true` | Abre o navegador ao iniciar |
| `csv_path` | `--csv` | `data/monitoramento_rota.csv` | Local das evidências |
| `stats_window` | | `120` | Ciclos usados nas métricas "recentes" e no diagnóstico |
| `history_points` | | `3600` | Pontos do gráfico mantidos em memória |
| `keep_sessions` | | `0` | Sessões arquivadas mantidas (`0` = todas) |
| `allow_remote_control` | | `false` | Permite alterar configurações a partir de outros dispositivos |

## Início automático com o sistema (opcional)

```sh
python portal_rede.py --instalar-autostart   # inicia sozinho a cada login, sem abrir o navegador
python portal_rede.py --remover-autostart
```

Usa a pasta "Inicializar" no Windows (com `pythonw`, sem janela), um LaunchAgent no macOS e o autostart
da sessão gráfica no Linux. Em servidores Linux sem interface gráfica, use um serviço systemd de usuário:

```ini
# ~/.config/systemd/user/monitor-rede.service
[Unit]
Description=Monitor de Estabilidade de Rede

[Service]
ExecStart=/usr/bin/python3 /caminho/do/projeto/portal_rede.py --no-browser
Restart=on-failure

[Install]
WantedBy=default.target
```

`systemctl --user enable --now monitor-rede` (e `loginctl enable-linger $USER` para rodar sem login).

## O painel

- **Status ao vivo**: online, instável (falhas ainda abaixo do critério de queda) ou queda em andamento,
  com cronômetro e o último salto que ainda responde.
- **Indicadores**: disponibilidade, tempo de sessão, quedas, tempo fora do ar, latência, jitter e perda no destino.
- **Diagnóstico** em linguagem simples: rede local × provedor, destino que bloqueia ping, latência instável.
- **Gráfico de latência** por salto (janela de tempo, média móvel, detalhe ao passar o mouse) com as quedas
  destacadas e a faixa de status do destino.
- **Perda por salto**, tabela de **quedas** (onde a falha ocorreu), **status por salto**, **eventos**
  (quedas, mudanças de rota, configurações) e informações de rota e ambiente.
- **Exportar evidências**: `.zip` com CSVs, resumo, relatório legível e snapshot JSON.
- **Sessões anteriores**: baixe as evidências de qualquer sessão arquivada.
- **Modo offline**: abra `web/dashboard.html` em qualquer computador e importe um `.zip` exportado
  (menu **Mais → Importar evidências**) para analisar sem o monitor rodando.
- Atualização em tempo real por Server-Sent Events, com recaída automática para consultas periódicas.
- Tema claro/escuro automático e layout para celular.

## Como interpretar

- **Queda** = o destino ficou sem responder por `outage_min_cycles` ciclos seguidos (padrão 3). Falhas
  isoladas contam apenas como perda de pacotes.
- **Onde foi a falha**: durante a queda o monitor registra o salto mais distante que continuou
  respondendo. "Nenhum salto respondeu" aponta para Wi-Fi/cabo, roteador ou o acesso do provedor;
  "após Hn" indica falha além desse ponto (provedor ou rota externa).
- **Perda só em saltos intermediários** normalmente é limitação de resposta ICMP dos roteadores e não afeta
  a conexão. O que importa é a perda que chega ao destino.

## Evidências geradas

Gravadas a cada ciclo em `data/` (ao iniciar uma nova sessão, a anterior vai para `data/sessoes/<início>/`):

| Arquivo | Conteúdo |
|---|---|
| `monitoramento_rota.csv` | Snapshot por salto (`Hop;Host_IP;Host_Name;Sent_pkt;Recv_pkt;Loss_Pct;Best_ms;Worst_ms;Avrg_ms;Last_ms;StDev_ms;Jitter_ms`) |
| `monitoramento_rota_quedas.csv` | Quedas (`Outage_ID;Start;End;Duration_Sec;Down_Cycles;Last_OK_Hop;Last_OK_IP;Scope`) |
| `monitoramento_rota_resumo.txt` | Resumo `chave=valor` (início, fim, quedas, tempo fora do ar, disponibilidade...) |
| `monitoramento_rota_latencia_log.csv` | Série temporal por salto (`Timestamp;Hop;Host_IP;Host_Name;Avrg_ms;Last_ms`) |
| `monitoramento_rota_eventos.csv` | Eventos (`Timestamp;Event;Detail`) |
| `monitoramento_rota_relatorio.txt` | Relatório legível para suporte/provedor |

Se um CSV estiver aberto no Excel, o monitor continua funcionando e grava os dados pendentes assim que o
arquivo for liberado. O log de latência cresce cerca de 70 MB por dia com intervalo de 1 s e 15 saltos; para
monitoramentos de vários dias, use um intervalo maior (2 a 5 s) ou `keep_sessions` para limitar o histórico.

## API local

Todas as rotas respondem JSON (com gzip quando aceito). Escrita só a partir da máquina local, com
`Content-Type: application/json`.

| Método e rota | Descrição |
|---|---|
| `GET /api/snapshot` | Estado completo: saltos, quedas, resumo, status, diagnóstico, rota, eventos, configurações |
| `GET /api/history?since=<instância>:<seq>` | Pontos novos do histórico de latência/status (incremental) |
| `GET /api/stream` | Server-Sent Events: snapshot + pontos novos a cada ciclo (retoma via `Last-Event-ID`) |
| `GET /api/health` | Versão, ambiente (SO, ping, traceroute), endereços de acesso, permissão de controle |
| `GET /api/config` | Configurações, campos editáveis e limites |
| `POST /api/config` | Altera configurações (`{"target": "1.1.1.1", "interval_sec": 0.5}`); trocar o destino inicia nova sessão |
| `POST /api/actions/rediscover` | Redescobre a rota agora |
| `POST /api/actions/reset` | Arquiva a sessão atual e inicia outra |
| `GET /api/export` | `.zip` com as evidências da sessão atual |
| `GET /api/sessions` | Sessões arquivadas |
| `GET /api/sessions/<nome>/export` | `.zip` de uma sessão arquivada |
| `GET /data/<arquivo>` | Arquivos de evidência da sessão atual |

## Modo console (sem painel)

```sh
python monitor_rota.py 8.8.8.8 --intervalo 2 --duracao-seg 3600
```

Mostra a tabela por salto no terminal e grava as mesmas evidências.

## Solução de problemas

| Sintoma | O que fazer |
|---|---|
| "Comando 'ping' não encontrado" (Linux) | `sudo apt-get install -y iputils-ping` (o `./iniciar.sh` oferece instalar) |
| "Ping local: FALHOU" | O sistema bloqueia ICMP para o usuário (comum em contêineres). Rode fora do contêiner ou conceda `CAP_NET_RAW` ao `ping`. |
| Destino nunca responde, mas a rota sim | O destino bloqueia ping. Troque para `8.8.8.8` ou `1.1.1.1` em Configurações. |
| Painel não abre em outro dispositivo | Inicie com `--lan` e libere a porta no firewall. |
| "Python não encontrado" no Windows | Instale pelo `iniciar.bat` (winget) ou em python.org marcando "Add python.exe to PATH". |

## Desenvolvimento

```sh
python -m unittest discover -s tests -t .   # testes (somente biblioteca padrão)
ruff check .                                 # lint opcional
node --check web/dashboard.js
```

A CI roda os testes em Windows, Linux e macOS (Python 3.9 a 3.13), incluindo o `ping` real do sistema.

Estrutura:

- `netmon/probes.py`: ping/rota multiplataforma e parsing independente de idioma
- `netmon/metrics.py`: métricas por salto, detecção de quedas e diagnóstico (domínio, sem I/O)
- `netmon/storage.py`: contratos de arquivos, arquivamento de sessões, relatório e ZIP
- `netmon/engine.py`: motor contínuo e resiliente compartilhado pelo portal e pelo console
- `netmon/config.py`: padrões, `config.json` e validação
- `netmon/autostart.py`: início automático por sistema operacional
- `portal_rede.py`: servidor HTTP, API REST/SSE e arquivos do painel
- `monitor_rota.py`: modo console
- `web/`: painel (HTML/CSS/JS sem dependências externas)

Regras de engenharia: [`docs/AI_CONTRACT.md`](docs/AI_CONTRACT.md), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md),
[`docs/DECISIONS.md`](docs/DECISIONS.md), [`docs/projectmap.md`](docs/projectmap.md) e `specs/`.

## Licença

MIT ([`LICENSE`](LICENSE)).
