# Atualização única: relatório e suspensão de admissões

Patch local autorizado em 31/10/2026. Git, deploy e restart ficam com o responsável.

## Conteúdo

Lista de trades: entry BRT | entry price | exit BRT | exit price | PnL % | net | entry EMA | entry MACD | exit EMA | exit MACD | exit reason | wave_open | marola90_open.
O nome exibido marola90_open lê marola_90_open persistido. Sem dado usa UNAVAILABLE; não reconstrói, não usa MAROLA60 ou rótulo de fechamento. Pareamento, cálculos e resumo não mudaram. A lista é a mesma também no modo trail_activation_gap; seus blocos específicos permanecem.

YAML: somente instrumentation.dmi15_trajectory_context_shadow.accept_new_entries e instrumentation.hs_bull_elastic_shadow.accept_new_entries passam a false. enabled permanece true. Não desabilitar os braços: posições restauradas continuam com suas regras de saída, inclusive elasticidade em LON, e seus estados/ledgers preservados. Braços drenando ainda consomem recursos; não é desligamento completo nem refutação científica.

## Um deploy, um restart

1. Fazer Git/preparar fontes como de costume; não executar ferramentas pesadas junto do bot. A correção leve de status da tarefa anterior pode integrar este mesmo pacote.
2. Parar o processo normalmente, uma única vez; confirmar que terminou antes de copiar arquivos/estados ou iniciar utilitários. Não apagar .pending, WAL/SHM nem alterar checkpoints. Preservar backup dos estados e ledgers no procedimento habitual, com bot parado.
3. Instalar o pacote completo e conferir as duas flags false, mantendo enabled=true. Não rodar migração MAROLA outra vez por causa deste patch: ele não exige reclassificação nem mudança de schema.
4. Iniciar uma única vez pelo comando habitual. Conferir sobrevivência do processo, posições/pairs restaurados, stops/ladder/peak/CB e ausência de novas admissões nos dois braços. Posições podem fechar normalmente após retomada; não confundir avanço legítimo com perda de estado.
5. O boot grava shadow_admissions_stopped em logs/system.log, com arm, stopped_at UTC em milissegundos, run_id, quantidade de abertas e existing_positions_continue=true. Ler de forma leve:

```bash
grep 'shadow_admissions_stopped' logs/system.log | tail -n 2
```

O evento indica a política carregada naquele run, não encerramento das posições ou sobrevivência posterior do processo. Confirmar o run atual e boot bem-sucedido. Enviar esses dois eventos com o horário do restart para registrar o marco BRT efetivo na MC. Não usar a hora de preparação local como interrupção real. Reboots seguintes repetem o evento, sem apagar marcos anteriores.

## Pendência explícita

Gravar MAROLA_90 no fechamento, análogo a wave_close, fica para outra rodada. Não implementado neste patch. Não preencher fechamentos antigos por inferência.

## Validação

45 testes passaram: relatório, suspensão/restauração dos dois braços e MAROLA. Testes de posição mostram que DMI continua até HARD_STOP e HS_BULL mantém elasticidade em LON e fecha ao perder contexto conforme a regra existente. Nenhuma chamada à VPS nesta tarefa.
