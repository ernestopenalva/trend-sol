# K1 shadow: validação operacional antes do deploy

Implementação preparada para `EMA_MACD_CLUSTER_ACT10_K1_SHADOW`. Git, deploy e
restart pertencem ao Ernesto. Este roteiro não autoriza nem executa essas ações.

## Paridade

Executar `venv\Scripts\python.exe -B tools\k1_shadow_parity.py` no Windows do workspace antes de levar
o código ao VPS. O recibo `data/analysis/ema_macd_cluster_act10_k1/PARIDADE_COMPLETA.json`
tem de registrar PASS nos paths LOW_FIRST e HIGH_FIRST. A referência local é o
`switch_run.py` recebido na pasta da proposta aprovada no Drive. A comparação
inclui cada campo econômico, admissões, estatísticas, quantidade de trades, net
total e bloqueios K1. O campo auxiliar `fast_evaluated` é registrado à parte:
ele existe no switch genérico por causa do FAST, que não integra estes braços.

## Memória na VPS de 961 MB

Coletar RSS/PSS por processo `main.py`, `MemAvailable`, swap e paginação do host
antes e depois. O coletor exige no mínimo 30 minutos de uptime e 5 minutos de
amostragem; a aprovação de memória exige pelo menos 192 MiB disponíveis e
ausência de crescimento contínuo de swap/paginação por 3 intervalos. Faça cada
coleta em uma pasta de evidência nova fora de `data/`, `logs/`, `config/` e `src/`.

No VPS, depois do sync de código feito pelo operador e antes do restart, com o
processo atual aquecido:

```bash
python3 tools/keyed_vps_check.py resources --root "$LIVE" --pid "$PID" \
  --phase before --output "$LIVE/backups/k1_memory_before"
```

Após o único restart feito pelo operador, aguardar pelo menos 30 minutos e
coletar novamente usando o PID novo e uma pasta vazia:

```bash
python3 tools/keyed_vps_check.py resources --root "$LIVE" --pid "$PID" \
  --phase after --output "$LIVE/backups/k1_memory_after"
```

Revisar `RESULT.json` dos dois estágios, inclusive RSS/PSS, mínimo de memória
disponível, delta de swap, `si/so` (contadores `pswpin/pswpout`) e PID estável.
Se a coleta pós-restart não atingir o critério, parar e reportar antes de
qualquer outra alteração. DMI15 e HS_BULL já estão configurados como
`enabled: false`; seus arquivos de estado, ledgers e históricos não devem ser
apagados nem substituídos.

## Marco de restart e cobertura

Registrar o instante exato do restart e então observar o log `structure_v2_enabled`,
`structure_health` e os ledgers. O boot deve declarar K1 habilitado; a rotina de
reparo de candles 5m/1h continua ativa. `forward_experiment_report.py` deve mostrar
K1 ao lado do braço base, com saídas defensivas, episódios e bloqueios K1. Para
memória e cobertura de rótulos por braço/fase:

```bash
python3 tools/keyed_vps_check.py observe --root "$LIVE" --since "$RESTART_ISO" \
  --output "$LIVE/backups/k1_observation"
```

O resultado de `observe` é evidência para revisão, não aprovação automática do
deploy nem justificativa para alterar regras.
