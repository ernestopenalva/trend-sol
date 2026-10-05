# CB: serialização de projeções por mutação

## Escopo e causa

Antes do patch, `_project_committed` reconstruía ledger e eventos completos
em cada commit para então comparar o conteúdo com o cache. A comparação já
evitava writes físicos idênticos, mas não evitava serialização nem alocação.

O patch introduz duas marcas voláteis por instância: `ledger` e `events`.
Não altera esquema persistido nem parâmetros econômicos.

## Mutação e commit

- `_append_closed_record` e `_update_closed_record` marcam ledger.
- `_append_audit_event` e `_update_audit_event` marcam eventos.
- A marca é colocada antes da mutação, inclusive se uma atualização falhar
  parcialmente. Não depende de tamanho de lista/registro.
- Os três appends de fechamento existentes (normal, FAST_DROP, fechamento
  experimental) e o update pós-fechamento do HS elástico usam esses helpers.
- Entradas/bloqueios/CB/rejeição de input atrasado passam pelo appender comum
  de eventos. Alterações de posições, peaks, stops e status continuam no
  checkpoint; não criam artificialmente uma entrada no ledger de fechamentos.
- Nenhuma marca é resetada ao começar um input. Só a projeção correspondente
  é limpa após write bem-sucedido ou confirmação de conteúdo idêntico.
- Falha parcial pode deixar ledger limpo e eventos dirty, se só o primeiro
  foi projetado. O tratamento de exceções/disable preexistente não mudou.

Continuam exatamente no caminho atual: `.pending`, processamento, sequência,
watermark/`last_input_ms`, `updated_at`, checkpoint por input, atomic replace,
fsync de arquivo e, em Linux, diretório.

## Restart, recovery e integridade

As marcas não são persistidas. Inicialização e carregamento de checkpoint
forçam ambas dirty: a verificação/reparação ocorre antes do replay de pending.
Assim, crash após checkpoint e antes da projeção continua recuperável a partir
do checkpoint autoritativo. Pending já commitado não é reaplicado.

A validação de prefixo/divergência permanece intacta. Atualizar um registro
que já foi projetado, incompatível com a regra de prefixo, continua falhando
como antes; este patch não autoriza reescrita de histórico divergente.

Toda futura mutação das duas listas ou de registros já incluídos deve usar
os helpers; para alterações aninhadas, passar o valor atualizado pelo helper
de update. Uma mutação direta nova que bypassasse esses pontos seria um risco
de manutenção. Os pontos atuais foram auditados em todo `src`.

## Braços e fluxos distintos

A infraestrutura aplica-se a REAL_A_CB, BE_OFF_CB, ACT20_GAP5, ACT10_GAP13,
FAST_DROP, HS_BULL, HS_BEAR, CB_EXIT_ALL, MACD_BU_MINUS, EMA_MACD e HIST_1M.
Nenhuma regra específica desses braços foi modificada.

REAL_A titular usa PositionRegistry/TradeLedger, não estas projeções completas.
BE030/BE_OFF e DMI também têm fluxo distinto. Não foram forçados a esta
abstração nem modificados. Testes de telemetria/REAL_A e a suíte geral foram
executados para verificar integração.

## Testes

`tests/test_cb_projection_dirty.py`: 13 testes novos cobrindo tick sem
serialização, journals/checkpoints preservados, append, update in-place,
fechamento, duas mutações no mesmo input, exceção parcial, falha de projeção,
divergência, restart, update commitado com projeção interrompida, recovery de
pending sem mutação, CB e independência entre instâncias.

Paridade antes/depois usa uma sequência fixa com entradas, HS múltiplos,
crise/cooldown, nova admissão, PL/TRAIL e restarts. O baseline força a visita
às duas projeções, reproduzindo o custo/comportamento anterior. Compara estado,
eventos e arquivos finais; ignora somente `updated_at` não determinístico.
As suítes existentes cobrem pending antes do checkpoint para entrada,
proteção e fechamento, late input e reparo após commit.

Execução final focada: **75 testes, OK**, nos módulos:

```
test_cb_projection_dirty
test_cb_forward_equivalence
test_circuit_breaker_shadow
test_forward_experiment_shadows
test_circuit_breaker_wiring
test_circuit_breaker_ladder_parity
test_cb_admission_equivalence
test_fast_drop_temporal_equivalence
test_trail_activation_gap_shadow
test_fast_drop_ema_shadow
test_ema_macd_hist_1m_shadow
test_exit_context_telemetry
```

Suíte completa final: **447 testes executados**. Não ficou integralmente verde:

- Erro conhecido: `DetectorAuditTests.test_replay_sources_match_frozen_revision`,
  comparação congelada com `0d3f9dc` rejeita `src/position/bot_full_engine.py`.
  Este arquivo não foi alterado pelo patch.
- Dois subcasos do teste de exit-context tiveram `WinError 5` intermitente em
  `os.replace` dos checkpoints temporários no Windows. O mesmo módulo passou
  na repetição isolada e na execução focada final. Não foi adicionado retry nem
  alterado atomic replace para contornar o ambiente.

## Benchmark controlado

Ferramenta: `tools/cb_projection_benchmark.py`. Usa cópias offline dos mesmos
11 checkpoints do diagnóstico, clones em diretórios temporários, warm-up
excluído, três rodadas de oito inputs por variante/modo e ordem alternada.
Nunca aponta os clones para estado real. Baseline visita sempre ambas as
projeções; after executa o dirty tracking efetivo, sem deepcopy/comparação de
históricos no caminho medido. Compara os checkpoints emitidos (exceto
`updated_at`), posições, clock/equity/CB, extras e conteúdo das projeções.

```
python tools/cb_projection_benchmark.py --snapshots data/analysis/cb_io_audit_20261005/snapshots --rounds 3 --inputs 8 --output data/analysis/cb_io_audit_20261005/dirty_tracking_benchmark_controlled.json
```

Por input, soma dos 11 braços:

| Medida | Antes | Depois |
|---|---:|---:|
| Total mediano, I/O real local | 632,55 ms | 429,22 ms |
| Total p90, I/O real local | 750,33 ms | 590,00 ms |
| Serialização de projeções, média | 231,39 ms | 0 ms |
| Total mediano, writes simulados | 443,54 ms | 223,29 ms |
| Serializações de streams de ledger/eventos | 22 | 0 |
| Chamadas JSON por registro projetado | 9.623 | 0 |
| Serializações de checkpoint | 11 | 11 |
| Writes de pending/checkpoint | 22 | 22 |
| Bytes de checkpoint | 15.532.995 | 15.532.995 |

**Economia mediana com I/O real local: 203,33 ms/input (32,14%).**
Sem writes reais: 220,26 ms/input (49,66%). Paridade: PASS em todas as rodadas.

No cenário steady, marcas novas de ledger/events: zero. Após o patch são
evitadas 11 serializações de ledger + 11 de eventos por input, correspondendo
a **14.768.835 bytes** de strings de projeção não reconstruídas. Esses mesmos
históricos continuam incluídos no checkpoint obrigatório.

Tempos médios das etapas na medição com I/O real local:

| Etapa | Antes | Depois |
|---|---:|---:|
| Processamento base do tick | 3,24 ms | 3,41 ms |
| Serialização de checkpoint | 214,28 ms | 269,81 ms |
| Persistência de checkpoint | 94,25 ms | 100,08 ms |
| Serialização de ledger/eventos | 231,39 ms | 0 ms |
| Persistência de ledger/eventos | 0 ms | 0 ms |
| Persistência de pending | 50,46 ms | 43,27 ms |

As oscilações de checkpoint/I/O refletem timings de parede variáveis, não
mudança de payload. Uma rodada anterior com testes concorrentes mostrou ganho
de disco muito menor; foi mantida como artefato, não usada para a estimativa
controlada. A redução de serialização foi consistente em ambas.

Os tempos são **locais Windows**, não tempos da VPS. Windows não mede fsync de
diretório Linux. `cpu_only` é tempo de parede com writes simulados, não uma
medição de CPU via process_time. O processamento base não representa todo o
callback do Monitor. Estes números não permitem prever o esvaziamento de
backlog nem a taxa de reconexões.

## Próximos custos observados e handoff

O **checkpoint continua sendo o maior custo remanescente**, com histórico
inteiro serializado e aproximadamente 15,53 MB regravados por input dos 11
braços. Pending/fsync permanece obrigatório. Inputs com eventos reais ainda
serializam a projeção alterada, e restart verifica ambas uma vez.

Nenhuma otimização adicional foi implementada: sem batching, compactação,
mudança de journal, threads, WebSocket, parâmetros ou estratégia. Não houve
commit, push, deploy, restart nem alteração de estado real. Publicação e
reinício ficam sob controle do usuário; não requer migração de estado.
