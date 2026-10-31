# MAROLA 60/90 — telemetria de abertura

Somente registro. `MAROLA_60` (`TREND_60M_2X30M_5M_V1`) e `MAROLA_90`
(`TREND_90M_2X45M_5M_V1`) derivam de `WAVE` por `dataclasses.replace`.
A classificação usa as mesmas funções `classify`/`StructureBuffer` do estudo,
sem nova implementação da geometria. Janelas de 12/18 candles 5m fechados,
duas metades de 6/9. MARÉ e ONDA não mudam. Conflito 45m continua apenas
telemetria herdada, sem efeito no rótulo.

## Campos

Para cada prefixo `marola_60` e `marola_90`:

- `*_open`: BULL / BEAR / EXPA / COMP / UNDEFINED / UNAVAILABLE;
- `*_open_at`: instante da abertura;
- `*_open_details`: janela, extremos, disponibilidade, versão e motivo;
- `*_open_source`: `ao_vivo` ou `reclassificado`;
- `*_rule_version`: versão exata acima.

O snapshot também declara `source`. Nenhum contexto de saída MAROLA é criado.
A camada comum `PositionBase` alcança REAL_A e os shadows; checkpoints,
eventos de posição e ledger conservam os campos. Restaurar uma posição antiga
não inventa um rótulo LIVE. Uma falha de telemetria vira UNAVAILABLE e não pode
interromper admissão, stop, reconciliação ou fechamento. Símbolos sem buffer
LIVE próprio recebem UNAVAILABLE, nunca candles SOL de outro instrumento.
Os braços ativos acompanhados usam SOLUSDT. O backfill atende todos os símbolos
dos ledgers históricos, inclusive os multimercado aposentados.

## Implantação — executada pelo responsável

Não foi feito deploy/restart por esta implementação. Não existe ainda marco
LIVE confirmado. Não confundir o timestamp do estudo ou deste patch com deploy.
Não há mudança de YAML econômico. Não há mudança de painel ou de relatórios.

1. Publicar os fontes pelo seu fluxo habitual de Git/deploy. Além dos dois
   fontes e da ferramenta abaixo, publicar `tools/trend_structure_migrate.py`
   e suas dependências existentes se ainda não estiverem na VPS; não substituir
   estado ou banco por dados locais.

   - `src/monitor/price_structure.py`
   - `src/app.py`
   - `tools/marola_telemetry.py`

2. Preparar o cache antes da parada, para reduzir indisponibilidade. Isso lê
   ledgers/checkpoints e só escreve no diretório de candles de análise. Reusa
   caches já baixados e pede à Binance pública somente os slots 5m faltantes.
   Uma linha parcial durante append pode exigir repetir o preparo. Nenhum
   ledger ou banco é alterado por `prepare`.

   ```bash
   python tools/marola_telemetry.py prepare --candles data/analysis/marola_live_candles
   ```

   Opcional: `--seed data/analysis/marola_20261030/market/5m.jsonl` se o cache
   auditado e seu `market_manifest.json` estiverem presentes na VPS: o SHA256
   do seed é conferido antes de qualquer uso. Só serve para SOL; cada outro símbolo tem
   seu próprio cache. Dados indisponíveis/delistados interrompem a migração:
   reportar o erro, não adotar candles de outro símbolo.

3. Parar normalmente pelo procedimento habitual, sem SIGKILL. Conferir ausência
   do processo e fechamento limpo de SQLite (a ferramenta rejeita WAL/SHM).
   Não apagar WAL/SHM ou `.pending` para fazer a verificação passar.

   ```bash
   pgrep -af '[p]ython.*main.py'
   python tools/marola_telemetry.py snapshot --output backups/marola_before.json
   python tools/marola_telemetry.py prepare --candles data/analysis/marola_live_candles
   python tools/marola_telemetry.py migrate --stopped --candles data/analysis/marola_live_candles --backup backups/marola_entry_v1
   python tools/marola_telemetry.py verify --backup backups/marola_entry_v1
   ```

   Todos os caminhos de backup/saída precisam ser novos. O segundo `prepare`
   completa os últimos trades. O snapshot é evidência, não substitui as cópias
   integrais da migração. Não seguir ao restart se `verify` falhar.

4. Reiniciar pelo seu procedimento habitual. O bootstrap reaproveita o mesmo
   5m do mercado real usado pelos sinais, e o handler o alimenta antes de
   liberar um sinal na fronteira. Não usa candle aberto. Não acrescenta um
   stream duplicado quando o 5m já está assinado.

   ```bash
   python tools/marola_telemetry.py snapshot --output backups/marola_after_restart.json
   python tools/marola_telemetry.py status --output backups/marola_live_start.json
   ```

   Comparar posições por pair_id, quantidades/reservas, stops, ladder e peak;
   CB, equity e pending closes devem sobreviver à restauração. Preços novos
   podem legitimamente mover stops/fechar posições após voltar a operar:
   conferir os respectivos eventos, não exigir estados congelados depois do
   restart. Durante a migração, os JSON de estado/pending ficam byte a byte
   intactos, e clocks/metadata/ordinais do SQLite não mudam.

5. Registrar o marco do evento `marola_live_enabled` em `logs/system.log`,
   com `enabled_at` em milissegundos, run_id e versões. `status` apresenta
   `enabled_at_brt` e a primeira abertura persistida `ao_vivo`, quando existir.
   São dois fatos distintos: habilitação e primeiro trade efetivamente gravado.
   Levar esse JSON, a revisão Git publicada e o manifesto de migração à MC.

## Backfill e durabilidade

`migrate` acrescenta somente os dez campos MAROLA de entrada. Não modifica
MARÉ, ONDA, timestamps, preços, PnL, motivos ou qualquer outro campo dos trades.
Abrange os JSONL em `data/trades` e apenas payloads `kind='ledger'` do histórico
CB SQLite: JSONL sozinho seria sobrescrito pela reconstrução da projeção.
Eventos de auditoria, pending, metadata, schema, ordinais e revisões não mudam.
Posições abertas e checkpoints não são reescritos. Campos `ao_vivo` existentes
são preservados, não convertidos em `reclassificado`.

Posições carregadas do período anterior ao marco não recebem uma abertura
`ao_vivo` fictícia. Caso fechem depois desta migração sem MAROLA de entrada,
entram numa rodada complementar de reclassificação dos ledgers, com novo backup
e processo novamente parado. Isso mantém os estados abertos intocados nesta tarefa.

A verificação completa ocorre antes de tocar os originais. Cópias duráveis,
staging, fingerprint de todos os outros campos e substituição atômica por
arquivo. Falha durante aplicação provoca rollback; uma interrupção abrupta
exige verificar o manifesto e, com o processo parado, executar:

```bash
python tools/marola_telemetry.py rollback --stopped --backup backups/marola_entry_v1
```

O rollback recusa desfazer histórico se o estado econômico já avançou. Não
restaurar backups antigos por cima de trades novos. A classificação histórica
assume disponibilidade no fechamento público; não reconstrói latência antiga.

## Conferência depois de alguns dias LIVE

Não há serviço ou automação instalado. Usar o timestamp ISO com fuso do marco:

```bash
python tools/marola_telemetry.py prepare --candles data/analysis/marola_live_candles
python tools/marola_telemetry.py compare --candles data/analysis/marola_live_candles --since "MARCO_ISO_COM_FUSO" --output data/analysis/marola_live_comparison.json
```

Código de saída 0 somente com ao menos uma observação e **100% de igualdade**.
Compara rótulo, versão, janela, últimos candles e extremos, nos ledgers e
posições persistidas. Cada observação em ledger/checkpoint é conferida, mesmo
se o trade tiver mais de uma representação. Zero observações não é aprovação.
Qualquer diferença fica integral no JSON e retorna código 1. Gap ou atraso
LIVE pode gerar UNAVAILABLE diferente da reconstrução pública: reportar, não
mascarar, deslocar janela ou reescrever o rótulo ao vivo para forçar paridade.

Paridade técnica com os snapshots do estudo congelado, sem replay econômico:

```bash
python tools/marola_telemetry.py study-parity --study data/analysis/marola_20261030 --output data/analysis/marola_study_parity_new.json
```

Versão/origem diferem deliberadamente do estudo; geometria, labels, extremos e
demais campos têm que ser idênticos. A conferência LIVE futura continua pendente
até existirem trades após o deploy. Nada desta tarefa cria validação científica
ou altera decisões de trading.
