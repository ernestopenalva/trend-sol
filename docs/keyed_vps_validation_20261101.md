# Preparação operacional — chaveados, 3 braços novos

Paridade aprovada por Ernesto nos seis casos: VIVO 1340/1341, VERDE 274/274,
EMA_MACD+CLUSTER+ACT10 1020/1021. A errata corrigida e as provas anteriores
permanecem preservadas. Git, publicação dos arquivos e execução são do operador.
O agente não acessou nem modificou o VPS. **Medições reais estão pendentes.**

## Critério aprovado e limites da coleta

- Comparar RSS/PSS por PID, MemAvailable e swap antes e depois de ativar os três
  braços. Começar cada janela com o processo rodando há pelo menos 30 minutos.
- MemAvailable deve permanecer em **pelo menos 192 MiB** durante a janela normal.
  Swap já ocupada e estável não reprova. Crescimento contínuo ou paginação
  constante reprova. A ferramenta sinaliza três intervalos consecutivos de
  10 segundos com crescimento da swap ou atividade pswpin/pswpout do host.
- A janela padrão é de 5 minutos (31 amostras), após o aquecimento. Tempos,
  intervalo, sequência e mínimo estão em `instrumentation.keyed_validation`.
  Os contadores de paginação são do host; se outro processo causar atividade,
  isso deve ser investigado, não atribuído automaticamente ao bot.
- Se falhar, preparar somente `enabled=false` de
  `dmi15_trajectory_context_shadow` e `hs_bull_elastic_shadow`, sem tocar em
  ledgers, checkpoints, SQLite ou históricos. Medir de novo após aquecimento.
  Se ainda falhar, parar e reportar antes de qualquer outra mudança.
- Um deploy e um restart no caminho normal. Se a insuficiência aparecer antes,
  o fallback pode integrar o mesmo deploy. Se aparecer depois, efetivar o
  fallback exige um novo stop/start no mecanismo atual; essa exceção operacional
  é executada pelo operador, nunca automaticamente por estas ferramentas.

O coletor também consome memória; a medida inclui essa pequena carga de forma
conservadora. Registrar quantas posições estão abertas: resultado com carteiras
vazias não demonstra o pico futuro com 15 posições nos três braços. Comparar
os estados/cargas observados, sem fabricar trades para aprovar memória.

## 1. Estagiar e conferir sem alterar o processo atual

Pacote local: `data/analysis/keyed_20261101_release/trend-sol-keyed-20261101.zip`.
Contém fontes, YAML preparado, ferramentas leves, recibos e manifesto. Não
contém `.env`, credenciais, estados, ledgers, bancos ou candles de laboratório.
Copiar/estagiar pelo procedimento do operador, em diretório novo no VPS, por
exemplo `/tmp/trend-sol-keyed-20261101`. Não extrair sobre o processo ativo.

Variáveis usadas nos comandos seguintes (Bash, VPS):

```bash
KEYED_LIVE=/root/trend-sol
KEYED_STAGE=/tmp/trend-sol-keyed-20261101
KEYED_PY=/root/trend-sol/venv/bin/python
KEYED_EVIDENCE=/root/trend-sol/backups/keyed_validation_$(date -u +%Y%m%dT%H%M%SZ)
pgrep -af '[p]ython.*main.py'
# Definir KEYED_PID com o PID confirmado do main.py; não escolher às cegas.
KEYED_PID=PID_CONFIRMADO
```

As pastas de saída devem ser novas. As ferramentas recusam sobrescrita.

```bash
"$KEYED_PY" "$KEYED_STAGE/tools/keyed_release_check.py" --root "$KEYED_STAGE" --compare-live-config "$KEYED_LIVE/config/config.yaml"
"$KEYED_PY" "$KEYED_STAGE/tools/keyed_vps_check.py" public-probe --root "$KEYED_LIVE" --config "$KEYED_STAGE/config/config.yaml" --output "$KEYED_EVIDENCE/public_probe"
"$KEYED_PY" "$KEYED_STAGE/tools/keyed_vps_check.py" resources --root "$KEYED_LIVE" --config "$KEYED_STAGE/config/config.yaml" --pid "$KEYED_PID" --phase before --output "$KEYED_EVIDENCE/before"
```

Exigir preflight e probe `passed=true`. O preflight compara hashes e recusa
diferenças operacionais não autorizadas no YAML do VPS (sem imprimir valores).
O probe faz somente três consultas públicas de klines, remove um slot **de um
buffer isolado**, recupera por REST e exige que o passado continue UNAVAILABLE.
Ele nunca instala esse provider no bot e nunca injeta falha no feed real.
Não lê `.env`, não usa cliente de execução e não envia ordens.

O baseline de memória deve estar aquecido. `passed=false` por processo frio,
coleta incompleta, PID trocado ou OOM exige inspeção; não autoriza o fallback
de memória automaticamente. Guardar todos os RESULT.json e samples.jsonl.

## 2. Um deploy e um restart pelo operador

Confirmar os resultados anteriores antes de publicar. O YAML local foi preparado
com os três novos braços `enabled=true` e `accept_new_entries=true`. DMI15 e
HS_BULL continuam, por padrão, **enabled=true, accept_new_entries=false**.

1. Fazer Git pelo fluxo habitual. Conferir o manifesto do pacote e o YAML do
   destino; publicar o conjunto inteiro de fontes compatíveis num único deploy.
2. Na sessão tmux já usada, parar o bot normalmente uma vez. Confirmar o término
   do PID. Preservar backup consistente dos arquivos operacionais com o bot
   parado, inclusive JSON, SQLite e respectivos WAL/SHM se existirem; não apagar
   arquivos para forçar validação e não restaurar dados locais sobre o VPS.
3. Aplicar fontes/configuração pelo procedimento habitual. Não executar migração,
   backfill, replay histórico ou limpeza de coorte: este patch não os exige.
   Não alterar saídas, capital ou tamanho dos braços existentes.
4. Iniciar uma única vez com o comando habitual. No procedimento documentado do
   projeto, é `bash scripts/rodar_monitor.sh` na sessão tmux existente.
   Nenhuma ferramenta desta entrega para ou inicia o processo.
5. Guardar o horário real UTC do restart e o evento `structure_v2_enabled`.
   Conferir `new_shadows` com os três enabled/accept_new_entries true, versões
   V2, run_id, `resource_fallback_shadows` e ausência de erros de tick/candle.
   Os chaveados aguardam janela completa antes de admitir entradas; falhas
   posteriores conservam o fallback explícito definido na v2.

O instante de preparação local não é marco LIVE. No sistema de arquivos, os
novos COHORT_STARTED/checkpoints são criados no startup efetivo, não agora.

## 3. Após pelo menos 30 minutos aquecido

Confirmar o novo PID, guardar a variável KEYED_EVIDENCE anterior e definir
KEYED_RESTART com o horário efetivo do evento, em ISO UTC com timezone:

```bash
KEYED_PID=NOVO_PID_CONFIRMADO
KEYED_RESTART=AAAA-MM-DDTHH:MM:SS.sss+00:00
"$KEYED_PY" "$KEYED_LIVE/tools/keyed_vps_check.py" resources --root "$KEYED_LIVE" --pid "$KEYED_PID" --phase after --output "$KEYED_EVIDENCE/after"
"$KEYED_PY" "$KEYED_LIVE/tools/keyed_vps_check.py" observe --root "$KEYED_LIVE" --since "$KEYED_RESTART" --output "$KEYED_EVIDENCE/live_coverage"
```

Conferir antes/depois por PID e `passed` da memória, os valores mínimo disponível,
delta de swap e sequência de paginação. O PID precisa sobreviver à janela;
o coletor recusa PID fora do diretório do projeto ou que não seja main.py.

No `live_coverage/RESULT.json`, examinar:

- `new_shadows_confirmed_enabled=true`, boot_marker correto e latest_health
  recente (cadência no YAML: 60 s; investigar atraso acima de duas cadências).
- Onda V2 e marola90 V2, slots faltantes e razões nos samples/repair_events.
  Após aquecimento, UNAVAILABLE persistente, worker_failed ou reparo sem
  recuperação é pendência operacional; não declarar a falta de candles resolvida.
- `health_wave`: taxa amostrada no tempo, com numerador/denominador explícitos.
  `arms[...].open/close`: taxa por fase de trade desde o restart; inclui
  fechamentos de carryover quando seu fechamento pertence à janela.
- Ausência de trades ou campos produz taxa `null`/cobertura ausente, não 0%.
  Registros históricos não são reclassificados. A leitura é streaming e não
  transacional; uma cauda ainda sendo escrita é identificada separadamente.

Se nenhum gap real ocorrer, registrar isso: o probe comprova a recuperação
isolada, mas não se pode alegar um reparo real que não foi observado. Encaminhar
os RESULT.json e amostras para revisão; não executar ferramentas históricas
pesadas junto do bot de 961 MB. Repetir coleta posteriormente para observar
crescimento normal de posições/históricos, sem reiniciar o processo.

## 4. Fallback condicionado, sem apagar histórico

Somente após janela completa e aquecida que reprove memória/paginação:

```bash
# Usar before/RESULT.json se a falha já existe antes do deploy;
# usar after/RESULT.json se a falha aparece após a ativação.
KEYED_FAILED="$KEYED_EVIDENCE/after/RESULT.json"
KEYED_SOURCE_CONFIG="$KEYED_LIVE/config/config.yaml"
# Se o deploy ainda não aconteceu, KEYED_SOURCE_CONFIG aponta ao YAML estagiado.
"$KEYED_PY" "$KEYED_STAGE/tools/keyed_fallback_config.py" --config "$KEYED_SOURCE_CONFIG" --evidence "$KEYED_FAILED" --output "$KEYED_EVIDENCE/fallback_prepared"
```

O utilitário apenas gera `config_fallback.yaml` e recibo; não o aplica. A diferença
semântica é exatamente os dois `enabled=false`. Antes do deploy inicial, conferir
o YAML alternativo com `keyed_release_check.py --root "$KEYED_STAGE"
--candidate-config .../config_fallback.yaml --fallback --compare-live-config ...`.
Nenhum outro parâmetro pode mudar. Se a falha surgiu após o deploy, aplicar as
mesmas duas flags por stop/start limpo realizado pelo operador.

Desligamento completo congela os shadows: não continuam gerenciando posições
abertas nem carregam seus checkpoints. Isso **não** liquida posições e não
apaga ledgers/histórico; registrar o marco e posições remanescentes antes de
parar. Não apresentar o período desligado como continuidade do experimento.

Depois de efetivar, aguardar novamente pelo menos 30 minutos e coletar:

```bash
KEYED_PID=PID_DO_FALLBACK_CONFIRMADO
"$KEYED_PY" "$KEYED_LIVE/tools/keyed_vps_check.py" resources --root "$KEYED_LIVE" --pid "$KEYED_PID" --phase fallback --output "$KEYED_EVIDENCE/fallback"
```

Exigir a mesma folga e estabilidade. Se falhar, **reportar e não fazer outra
mudança**. O preparador recusa usar uma falha de fase fallback para propor novas
alterações. Nenhum fallback foi aplicado pelo agente.
