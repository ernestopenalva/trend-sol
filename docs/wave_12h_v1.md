# Entrega A — ONDA12h (`TREND_12H_2X6H_15M_V1`)

## Escopo e arquitetura

Somente telemetria. MARÉ `TREND_72H_2X36H_V1`, gates, sizing, CB, stops,
ordem das decisões e YAML econômico permanecem intactos. Não há novo braço.
Não executada Entrega B nem PnL segmentado pela ONDA.

O mesmo classificador (`src/monitor/price_structure.py`) recebe uma especificação:
MARÉ=72×1h, metades36, conflito<4h; ONDA=48×15m, metades24, conflito<45min.
Maiores highs/menores lows por metade; empate intrametade escolhe o candle mais
antigo. HH+HL=BULL, LH+LL=BEAR, HH+LL=EXPA, LH+HL=COMP. Igualdade entre extremos
das metades=UNDEFINED/EQUAL_EXTREME. Ausência/gap/inválido=UNAVAILABLE na ONDA,
com motivo e slots ausentes. Não deslocar janela; não substituir extremos.

O cache15m é independente dos indicadores econômicos e reaproveita o stream
existente. Se15m não estiver assinado, o stream adicional é exclusivamente
observacional. Warm-up ocorre na inicialização, reaproveitando o fetch15m quando
ele já existe. Não há rede, DB nem escrita adicional nos OPEN/CLOSE para classificar.
O custo de captura é compartilhado/cacheado; não se constrói histórico por posição.

LIVE registra a primeira disponibilidade no buffer, em milissegundos locais; uma
consulta anterior a esse recebimento não utiliza o candle. Duplicação após
reconexão não altera a primeira disponibilidade. Candles em formação são ignorados.
Erro de ONDA não impede MARÉ nem transição econômica. Gaps no feed tornam a ONDA
UNAVAILABLE até os48 slots exigidos existirem novamente; não há reparação REST
síncrona nem estado stale silencioso. Isso pode manter ONDA indisponível por até
12h após uma lacuna não reparada. É explícito e não bloqueia trading.

BACKFILL/REPLAY usam a mesma função e disponibilidade no fechamento público;
não afirmam reproduzir a latência histórica efetiva de entrega ao processo.
Paridade refere-se à mesma informação causal disponível. Isso não prova que um
processo que recebeu dados atrasados teria tido o mesmo snapshot ao vivo.

## Campos e consumidores

`wave_open`, `wave_open_at`, `wave_open_details`, `wave_close`, `wave_close_at`,
`wave_close_details`, `wave_rule_version`. Details guarda versão, origem, janela,
último/esperado fechado, quatro extremos e horários, HH/LH/HL/LL, distâncias em
minutos/horas, conflitos45m e motivo de indisponibilidade/empate. Timestamps dos
extremos são aberturas de candles, não instante intrabar do high/low.

Abertura é imutável; fechamento recalculado. Aberta:wave_close=OPEN. Restore não
inventa entrada LIVE de uma posição anterior ao restart. `wave_close` nunca é
input de admissão. Campos seguem o fluxo existente de posição, ledger, eventos e
checkpoint incremental, sem mudar durabilidade econômica.

Listas relevantes do forward report passam a mostrar:
`trend_open | wave_open | trend_close | wave_close`.
O `--trend-audit` existente mostra evidências de ambas; cobertura ONDA discrimina
BULL/BEAR/EXPA/COMP/UNDEFINED/UNAVAILABLE/OPEN. Tabela econômica global inalterada.
As ferramentas do antigo estudo K3 são arquivos históricos, não consumidores
operacionais da definição atual; não foram reescritas.

## Artefatos locais e diagnóstico

Diretório `data/analysis/wave_12h_20261017/`:

- `SOLUSDT_15m.jsonl`: cache auditado preservado e faltante público completado.
- `wave_backfill_delivery.jsonl` e manifesto: reconstrução causal local desde
  08/10/2026 23:52 BRT, incluindo carryovers e abertas.
- `diagnostic_final/RELATORIO.md`: diagnóstico completo, sem finanças.
- JSONs de estabilidade, episódios, transições diárias, concordância, timestamps
  e seis exemplos PNG com os quatro extremos nas duas escalas.
- `migration_rehearsal/validation.json`: ensaio em NOVA cópia, idempotência,
  invariância de todos os campos fora de wave_*, incluindo MARÉ, e rollback exato.

Corte de mercado:01/06/2026 00:00→17/10/2026 01:00 BRT, último1h encerrado
01:00 e último15m encerrado01:00. Os candles15m adicionais não ultrapassam o
corte do cache1h já disponível no laboratório. Fontes/hashs constam no manifesto.
População de trades é cópia local da entrega anterior; não é coleta da VPS agora.
Posições de braços simultâneos são correlacionadas, não N independente.
Fechamentos posteriores ao corte não entram na estabilidade fechada; são censurados.

## Atualização na VPS — comandos para execução pelo usuário

Nenhum deploy, restart ou migração foi executado na VPS nesta entrega.
O usuário publica os fontes pelo procedimento habitual. NÃO enviar estados, DBs,
backup de laboratório ou sidecar antigo por cima da produção.

Enviar SOMENTE candles públicos (PowerShell local):

```powershell
scp C:/dev/trend-sol/data/analysis/wave_12h_20261017/SOLUSDT_15m.jsonl root@207.154.197.12:/root/trend-sol/data/analysis/wave_12h_seed.jsonl
```

Parar o bot pelo procedimento habitual e manter parado até VERIFIED. Confirmar
ausência de processos do bot e fechamento limpo dos SQLite (WAL/SHM provocam recusa;
não apagá-los). Na VPS, com venv ativo e diretório `/root/trend-sol`:

```bash
python tools/wave_structure_backfill.py --candles data/analysis/wave_12h_seed.jsonl --extend-until "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --output data/analysis/wave_apply_v1/SOLUSDT_15m.jsonl
python tools/wave_structure_backfill.py --root /root/trend-sol --candles data/analysis/wave_apply_v1/SOLUSDT_15m.jsonl --output data/analysis/wave_apply_v1/fresh_backfill.jsonl
python tools/forward_experiment_report.py --since "08/10/2026 23:52" > data/analysis/wave_apply_v1/report_before.txt
python tools/trend_structure_migrate.py --root /root/trend-sol --layer wave --candles data/analysis/wave_apply_v1/SOLUSDT_15m.jsonl --backup /root/trend-sol/backups/wave_12h_v1 --offline-confirmed
python tools/trend_structure_migrate.py --root /root/trend-sol --backup /root/trend-sol/backups/wave_12h_v1 --verify --offline-confirmed
python tools/forward_experiment_report.py --since "08/10/2026 23:52" > data/analysis/wave_apply_v1/report_after.txt
diff -u <(sed '/TREND COVERAGE/,$d' data/analysis/wave_apply_v1/report_before.txt) <(sed '/TREND COVERAGE/,$d' data/analysis/wave_apply_v1/report_after.txt)
python tools/forward_experiment_report.py --experiment ema_macd --list-accepted --trend-audit --since "08/10/2026 23:52"
```

Exigir APPLIED, depois VERIFIED, invariância econômica e nenhuma diferença na
tabela econômica. A migração wave permite alterar SOMENTE os sete camposwave;
os campos de MARÉ integram o hash de invariância e não são recalculados. O sidecar
de análise preparado não substitui a migração atual. Ele serve para auditoria.

Se diretórios de preparação/backup já existirem, usar novos nomes; não apagar
backup anterior nem reutilizar manifesto de outra migração. O comando de backfill
não sobrescreve artefato: com manifesto, hashes e inputs idênticos retorna no-op;
mudança de input ou saída incompleta exige novo destino. Reconstrução em outro
destino produz o mesmo conteúdo para os mesmos inputs. A migração aplicada ao
mesmo estado é no-op.

Backup guarda originais, staging e manifesto durável. Troca atômica por arquivo,
não transação multiarquivo. Falha capturada faz rollback automático. Interrupção
abrupta pode deixar PREPARED: NÃO reiniciar; executar rollback parado e verificar.
Se interrompida antes de existir manifesto, não houve substituição operacional;
preservar o diretório parcial para inspeção e usar outro backup na nova tentativa.

Rollback, somente com bot parado e sem avanço econômico posterior:

```bash
python tools/trend_structure_migrate.py --root /root/trend-sol --backup /root/trend-sol/backups/wave_12h_v1 --rollback --offline-confirmed
```

Nunca restaurar estados antigos sobre trades novos: o rollback recusa avanço
econômico. Após validação, o usuário reinicia manualmente e confere o próximo
OPEN/CLOSE com `wave_*_details.source=LIVE` e a versão correta. BACKFILL não comprova
captura LIVE. No primeiro evento, cache insuficiente deve ser explícito, não oculto.

## Entrega B reservada

Não iniciada. Futura análise só exploratória, com sinais concordantes HIGH/LOW,
incerteza por episódios, concordância entre metades temporais e pelo menos10
episódios por célula. Não se transfere nenhum resultado estrutural como validação
econômica; observação futura independente continuará necessária.

## Arquivos e testes desta entrega

Implementação: `src/monitor/price_structure.py`, `src/app.py`.
Leitura/apresentação: `tools/price_structure_report.py`,
`tools/forward_experiment_report.py`, `tools/forward_matrix_audit.py` (somente
colunas das listagens, sem executar nem alterar a análise econômica).
Reconstrução/migração: `tools/price_structure_backfill.py` (loader/paridade
parametrizados), `tools/trend_structure_migrate.py` (escopo opcionalwave,
compatibilidade com manifestos antigos preservada).
Novos: `tools/wave_structure_backfill.py`, `tools/wave_structure_diagnostic.py`,
`tools/wave_structure_validate.py`, `tests/test_wave_structure.py`, este documento.
Testes atualizados: `tests/test_price_structure_telemetry.py`,
`tests/test_forward_experiment_report.py`.

As alterações previamente existentes em `docs/trend_2x36h_v1.md`,
`tests/test_trend_2x36h.py` e na versão incremental do migrador foram preservadas.

75 testes focados aprovados nos módulos wave_structure, trend_2x36h,
price_structure_telemetry, forward_experiment_report e forward_matrix_audit. Validação cobre quatro
estados, empates, candles abertos, timestamps não alinhados,45min estrito,
disponibilidade tardia, gaps, duplicatas após reconexão, restore, paridade de
origens, invariância sintética de trading, paridade da MARÉ com snapshots
arquivados, idempotência, falha parcial/rollback, preservação de MARÉ/SQLite e
apresentação. O fixture de relatório fecha explicitamente o SQLite criado no
teste, evitando handle aberto durante limpeza no Windows, sem mudar runtime.

Backfill local:1.448 identidades únicas,1.432 fechadas/16 abertas;13.505 snapshots
com paridade de origens, zero UNAVAILABLE por falta de candles. Na estabilidade,
1.412 encerraram até o corte de mercado e20 fechamentos posteriores são censurados;
61 pares têm UNDEFINED por empate em pelo menos uma ponta e são separados.
Ensaio em cópia:39 arquivos/11 SQLite,VERIFIED, resumo econômico idêntico,
hash de todos os campos fora de wave_* idêntico (inclui MARÉ), idempotência e
rollback byte a byte. Originais do laboratório intocados.
