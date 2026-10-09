# Persistência CB v3 — engenharia, migração e rollback

Nenhuma regra econômica, threshold, admissão, stop ou prioridade de decisão foi
alterada. YAML permanece intacto. Não fazer deploy/restart automaticamente.

## Antes

Por braço CB/input: JSON pendente atômico + checkpoint atômico contendo todo
`closed_records` e `audit_events`. Cada arquivo exigia fsync de arquivo e
diretório no Linux: quatro sincronizações por braço, 44 para os 11 CB.
As projeções dirty eram reserializadas/regravadas integralmente. Histórico
crescia no custo de cada input, inclusive sem posição aberta.

## Depois: ordem de durabilidade

```
input no WAL SQLite (FULL)
  -> mesma decisão/mutação econômica
  -> mudanças de histórico no WAL (FULL), quando houver
  -> checkpoint JSON enxuto: arquivo fsync -> rename -> diretório fsync
  -> append somente do sufixo novo das projeções JSONL
```

O arquivo `<braço>.history.sqlite` é um journal **por braço**, com tabelas:

- `pending`: uma linha substituível contendo o input e sequence;
- `history`: registros incrementais, indexados por tipo/ordinal/revision.

A metadata de identidade impede usar o journal de outro braço. No shutdown
normal, o app fecha os journals após terminar o loop de inputs, sem novo save de
estado. No crash abrupto, o WAL existente continua parte da recuperação.

Não se reserializa/regrava a história existente. Não se guarda um snapshot
integral dela em outra coluna/arquivo a cada input. SQLite atualiza páginas
pequenas; WAL/autocheckpoint podem acrescentar sincronizações ocasionais.

Checkpoint `cb_schema=3`: posições e suas proteções, clocks/watermark/sequence,
CB/equity/peak/history rolling de 4h, pending closes, buckets/contadores,
contexto corrente e históricos **curtos necessários à causalidade**,
market_points limitados, extras vivos de cada variante, `history_revision` e
`history_counts`. Os históricos completos deixam o JSON, não a auditoria.

Os atributos históricos em memória permanecem para consumidores existentes e
reconciliação. Eles não são mais serializados integralmente no caminho quente.

## Crash/restart

- Antes do commit do input: não houve decisão financeira; um input ainda não
  durável não é recuperável por este protocolo (mesma fronteira anterior).
- Depois do input e antes do checkpoint: restaura-se o estado comprometido;
  revisões do histórico posteriores ao marker são descartadas; o input durável
  é reavaliado exatamente uma vez, com as mesmas validações temporais.
- Depois do checkpoint: histórico já é durável. O pending com sequence já
  comprometida não é reaplicado. Projeções ausentes/atrasadas são reparadas.
- Append JSONL interrompido: só se trunca a linha final incompleta quando o
  conteúdo existente é prefixo exato do histórico comprometido. Divergência
  não é sobrescrita: falha fechada exige reconciliação.
- Histórico faltante, count inconsistente, duplicação ou equity divergente:
  não inicializa capital artificialmente; falha de reconciliação.
- Reescrita de linha histórica já projetada continua proibida. Anotações de
  um novo fechamento, antes do commit, continuam permitidas e agrupadas.

A durabilidade depende do filesystem/dispositivo honrar sync. Os testes de
SIGKILL verificam crash de processo, **não simulam falha elétrica do disco**.

## Sincronizações

Input ordinário CB sem mudança histórica:

- antes: 4 por braço;
- depois: 1 commit WAL FULL + 2 do checkpoint = 3 por braço;
- 11 CB: 44 -> 33, confirmado por strace no Linux local.

Com eventos/fechamentos: um commit adicional agrupa as mudanças de ledger e
eventos no journal; cada projeção alterada recebe um append/flush/fsync.
Criação, migração, teardown e WAL autocheckpoint não são o caso steady-state.
Não se promete que todo input, de qualquer tipo, tenha exatamente 33 syncs.

FULL em WAL sincroniza cada commit; a documentação distingue esse modo de
NORMAL, que não oferece a mesma durabilidade por commit:
[SQLite synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous),
[SQLite WAL](https://www.sqlite.org/wal.html).

## Migração

Leitura aceita schema 2 e 3. Na primeira leitura de schema 2, antes de migrar,
preservam-se cópias duráveis de checkpoint, `.pending`, ledger e eventos, quando
existirem, com sufixo `.pre-v3`. Não são sobrescritas. Uma migração repetida
com conteúdo diferente preserva também `.pre-v3.<sha256>`.

A história do schema 2 é importada uma vez. O checkpoint antigo permanece
autoritativo até o primeiro commit v3. Pending legado é preservado e tratado
pelas mesmas regras de sequence/watermark; conflito entre journals falha.

Schema 3 requer o journal: copiar só o JSON não constitui backup de recuperação
ou dataset histórico completo. Se houver WAL ativo, a cópia deve incluir o WAL
ou usar backup consistente SQLite. Preferir backup com o writer parado pelo
operador. Não apagar `.pending`, `.sqlite`, `-wal` ou `-shm` para "destravar".

## Rollback sem perder o período pós-migração

O operador controla todas as execuções na VPS. Procedimento offline:

1. Parar o writer por decisão do operador; preservar o conjunto atual
   JSON/journal/WAL/projeções e os backups pré-migração.
2. Para **cada um dos 11 CB**, exportar para um diretório novo:

   ```text
   python tools/cb_persistence_rollback_export.py --state data/state/<braço>.json --output <diretório-novo-por-braço>
   ```

3. A exportação é read-only na origem e produz checkpoint schema 2 com toda a
   história comprometida atual, ledger.jsonl, events.jsonl e pending legado.
   Instalar manualmente esses arquivos nos caminhos definidos no YAML para
   esse braço, com a versão anterior do código. Não misturar braços.
4. Preservar o checkpoint atual e verificar sequence, ledger/event counts,
   posições, equity/peak e pending. O código anterior recupera o pending
   quando sua sequence ainda não foi comprometida.
5. Só o operador decide publicar/reiniciar. Após startup, revisar reconciliação.

**Não** restaurar simplesmente `.pre-v3` depois de já existirem novos trades:
isso voltaria o estado ao passado e perderia resultados posteriores. Exportar
o estado comprometido atual é necessário. Rollback de código e de dados deve
ser coordenado; a versão antiga não lê schema 3.

## Outros caminhos

- REAL_A: `open_positions.json` não inclui ledger completo; é regravado por
  tick e em transições críticas (incluindo EXIT_PENDING). Mantidos conteúdo,
  cadência, clientOrderId, reserva de SOL e reconciliação. Somente JSON compacto.
  Zero fsync anterior continua zero neste caminho; não se atribui a ele a
  garantia nova dos CB. `cycle_state.json` contém históricos de deduplicação,
  mas é gravado em eventos de ciclo/fechamento, não por aggTrade. Não removidos.
- BE_OFF: checkpoint de posições/buckets/contexto curto, sem histórico completo
  do ledger. JSON compacto via base ContextShadow; cadência preservada.
- H2: posições/metadados vivos/buckets; metadados de fechados já eram removidos.
  JSON compacto; nenhuma mudança de sizing/cadência.
- DMI15_TRAJECTORY_CONTEXT_SHADOW: base ContextShadow compacta, sem ledger
  integral. Variantes DMI15 desativadas não receberam refatoração de estratégia.

## Ferramentas e compatibilidade dos consumidores

`forward_experiment_report.py` continua lendo ledgers/eventos e posições, sem
depender dos históricos completos dentro do JSON. Não mudou sua apresentação.

`read_cb_checkpoint(path)` fornece uma visão **read-only** schema 2/3 com
histórico reidratado para auditorias; não expande o checkpoint em disco.
Circuit breaker report e winner readout usam essa visão. Ferramentas de estudos
congelados com snapshots schema 2 permanecem válidas nesses snapshots; scripts
que indexam `closed_records/audit_events` diretamente devem usar a visão ou
as projeções JSONL para novos datasets. Missing journal não vira lista vazia.

`tools/cb_persistence_validation.py` executa benchmark offline dos 15 braços,
paridade histórica e testes. O baseline é uma cópia congelada do módulo anterior
com SHA256 nos resultados. Não cria ordens/rede/deploy. Paridade econômica usa
codec/IO físico simulado, mas executa a máquina de estados, builders de
checkpoint e transações incrementais em memória. Codec/IO e recuperação real
são validados separadamente em benchmark e SIGKILL.

No Windows, executar os testes legados pelo modo `--mode tests` dessa ferramenta:
ele fecha handles SQLite pertencentes ao diretório temporário antes de removê-lo.
Esse adaptador é somente do runner de testes; não altera decisões nem fecha o
journal durante os inputs testados. No runtime, o journal permanece aberto até
shutdown para preservar batching/custo de sync. SIGKILL real é executado no
Linux local/WSL, não substituído por exceção Python no Windows.

Resultados e limitações quantitativas:
`data/analysis/persistence_v3/RELATORIO.md`.
