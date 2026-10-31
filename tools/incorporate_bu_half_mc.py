"""Single-operator documentary incorporation requested by Ernesto; no trading."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import json,hashlib,re,shutil,argparse,zipfile
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_bu_half_20261027';W=S/'incorporacao_mc'
MC=Path('G:/Meu Drive/trend-sol/Memória_Científica');ORIG=MC/'propostas/MC-PROP-20261027-BU-HALF-01';PROP='propostas/MC-PROP-20261027-INCORPORACAO-BU-HALF-01'
EXP='experimentos/MC-EXP-0007_BU_HALF.md';DEC='decisoes/MC-DEC-0016_INCORPORACAO_BU_HALF.md';ADM='metodologia/MC-ADM-0015_INCORPORACAO_BU_HALF.json';FON='experimentos/versoes/MC-EXP-0007_BU_HALF_FONTES_V1'
L4='linhas/MC-LIN-0004_EXPOSICAO_E_PROTECAO_CARTEIRA.md';L3='linhas/MC-LIN-0003_FILTROS_ENTRADA_E_ADMISSAO.md'
AUTH={'autoridade':'Ernesto, responsável pelo Trend-Sol, usuário desta conversa','data_registro':'2026-10-27','origem':'Pedido explícito nesta conversa: atualiza mc com o resultado desse estudo','escopo':'Incorporação documental dos resultados BU_HALF, vínculos nas linhas existentes e índice; sem homologação científica, alteração de hipótese, encerramento de variante ou autorização operacional.'}
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def j(x):return json.dumps(x,ensure_ascii=False)
def head(meta,body):return '---\n'+'\n'.join(k+': '+j(v) for k,v in meta.items())+'\n---\n\n'+body.strip()+'\n'
def save(rel,data):
 p=W/'preparados'/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data.encode('utf-8') if isinstance(data,str) else data)
def base_meta(id,tipo,summary=None):return {'id':id,'tipo':tipo,'versao':1,'status_documental':'INCORPORADO_FACTUAL' if tipo!='DECISAO' else 'APROVADO_ESCOPO_DOCUMENTAL','data_incorporacao':'2026-10-27','autor':'Codex','aprovacao_humana':AUTH,'revisao_cientifica':'REVISAO_NAO_INDEPENDENTE','estado_cientifico':None,'condicao_administrativa_cientifica':'PENDENTE_DE_HOMOLOGACAO','resumo_executivo':summary,'decisao_incorporacao':'MC-DEC-0016','manifesto_externo':ADM}
def updated(rel,version_path,append,insert=None):
 raw=(MC/rel).read_bytes();text=raw.decode('utf-8-sig');assert text.startswith('---\n') or text.startswith('---\r\n');text=text.replace('\r\n','\n');_,meta,body=text.split('---',2)
 old_version=int(re.search(r'^versao:\s*(\d+)',meta,re.M).group(1));meta=re.sub(r'^versao:\s*\d+',f'versao: {old_version+1}',meta,flags=re.M)
 for key,value in [('versao_anterior',version_path),('sha256_versao_anterior',hashlib.sha256(raw).hexdigest())]:
  line=key+': '+j(value)
  if re.search('^'+key+':',meta,re.M):meta=re.sub('^'+key+':.*$',lambda _:line,meta,flags=re.M)
  else:meta+='\n'+line+'\n'
 meta+='\naprovacao_atualizacao_bu_half: '+j(AUTH)+'\n'
 for datekey in ['data_atualizacao','data_atualizacao_documental']:
  if re.search('^'+datekey+':',meta,re.M):meta=re.sub('^'+datekey+':.*$',lambda _:datekey+': "2026-10-27"',meta,flags=re.M)
 if insert:
  anchor,addition=insert;assert body.count(anchor)==1;body=body.replace(anchor,addition+'\n\n'+anchor)
 body=body.rstrip()+'\n\n'+append.strip()+'\n' if append else body
 save(version_path,raw);save(rel,'---'+meta+'---'+body)
 return {'path':rel,'sha256_before':hashlib.sha256(raw).hexdigest(),'version_preserved':version_path,'version_before':old_version,'version_after':old_version+1}

def prepare():
 W.mkdir(exist_ok=True)
 assert not (W/'PLANO.json').exists(),'Preparation already frozen'
 for rel in [EXP,DEC,ADM]:assert not (MC/rel).exists(),rel
 assert not (MC/PROP).exists(),'Incorporation proposal already exists'
 pkg=json.loads((S/'PACOTE_SHA256.json').read_bytes());zipname='replay_bu_half_20261027.zip';assert sha(ORIG/zipname)==pkg['sha256']==sha(S.parent/zipname)
 with zipfile.ZipFile(ORIG/zipname) as z:
  assert z.testzip() is None;assert len([n for n in z.namelist() if n.startswith('runs/') and n.endswith('.json')])==8
  for name in ['CLASSIFICACAO.json','RESUMO.json','COMPARACOES.json','DIVERGENCIAS.json','AUDITORIA_ENTREGA.json','UNIFORME_CALIBRACAO.json']:
   data=z.read(name);assert hashlib.sha256(data).hexdigest()==sha(S/name);save(FON+'/'+name,data)
 classification=json.loads((S/'CLASSIFICACAO.json').read_bytes());assert classification['classification']=='INCONCLUSIVA';assert json.loads((S/'AUDITORIA_ENTREGA.json').read_bytes())['passed']
 source_names=['PREREGISTRO_V1.md','PRE_REGISTRO_HASH.json','MANDATO_HUMANO.txt','PROPOSTA_RESULTADOS.md','RELATORIO_BU_HALF.md','LEITURA_CIENTIFICA.md','REPRODUCAO.md','NOTAS_IMPLEMENTACAO_ANTES_ANALISE.md','NOTAS_EXECUCAO.md','TABELA_ECONOMICA.csv','COMPARACOES.csv','BLOCOS_HS.csv','TRADES_TODOS.csv']
 source_manifest=[]
 for name in source_names:
  assert sha(ORIG/name)==sha(S/name),name
  save(FON+'/'+name,(ORIG/name).read_bytes());source_manifest.append({'origem':'propostas/MC-PROP-20261027-BU-HALF-01/'+name,'destino':FON+'/'+name,'bytes':(ORIG/name).stat().st_size,'sha256':sha(ORIG/name)})
 # Original prospective hash helper remains available at its historical path.
 save(FON+'/versoes/PRE_REGISTRO_HASH_ORIGINAL.json',(ORIG/'versoes/PRE_REGISTRO_HASH_ORIGINAL.json').read_bytes())
 def ref(name):return {'arquivo':FON+'/'+name,'sha256':sha(W/'preparados'/FON/name)}
 meta=base_meta('MC-EXP-0007','EXPERIMENTO');meta.update(linha='MC-LIN-0004',vinculos=['MC-LIN-0003','MC-HIP-0002','MC-EXP-0006'],objeto='REGRA_IDEALIZADA / REPLAY_SISTEMICO',origem_proposta={'arquivo':'propostas/MC-PROP-20261027-BU-HALF-01'},pre_registro=ref('PREREGISTRO_V1.md'),relatorio_integral=ref('RELATORIO_BU_HALF.md'),leitura_cientifica=ref('LEITURA_CIENTIFICA.md'),classificacao_original='INCONCLUSIVA',validade_homologada=None,homologacao_cientifica=False,equivalencia_runtime_demonstrada=False,pacote={'arquivo':'propostas/MC-PROP-20261027-BU-HALF-01/'+zipname,'bytes':pkg['bytes'],'sha256':pkg['sha256']},criterios_originais=classification['gates'])
 body='''# BU_HALF — experimento incorporado documentalmente

Incorporação solicitada por Ernesto em 27/10/2026. Preserva o resultado original **INCONCLUSIVA**, sem promover a hipótese a estado científico homologado. A autorização é documental; não encerra a política, não escolhe uma estratégia e não autoriza forward ou operação. Este estudo é distinto do STACK_GUARD_2NEG e da hipótese FAST_DROP ligada ao seletor MACD.

## Objetivo, desenho e população

Investigar a redução seletiva das entradas BU− para 10 USDT contra o bloqueio integral, o controle BE_OFF_CB de 20 USDT e UNIFORME calibrado pelo notional negociado. Quatro carteiras próprias em LOW_FIRST (principal) e HIGH_FIRST (sensibilidade), oito runs. Janela: 01/06/2026 00:00 até 27/10/2026 22:00:59,999 BRT; ponto médio em 14/08/2026 11:00:30 BRT.

Custos BASE: 3,5 bps de fricção por lado e taxa de 0,10% por lado. Mantidos saídas, CB, slots, bucket, spacing e limite de cinco posições. Posições abertas entram com MTM líquido até END; sem liquidação forçada. Capital de 100 USDT nominal, sem cashgate. Net por 100 = net total / soma dos notionals de ENTRADA × 100, incluindo abertos; não é rentabilidade de uma conta autofinanciada nem turnover de compra e venda contado duas vezes.

UNIFORME foi fixado uma única vez em **17,19646799116998 USDT**: 31.160 USDT do BU_HALF LOW_FIRST divididos pelos 1.812 trades BE_OFF_CB LOW_FIRST. Valor aplicado aos dois paths e a todos os recortes, sem recalibração. O volume efetivo de UNIFORME foi 31.160,000000 USDT em LOW_FIRST e 31.177,196468 em HIGH_FIRST; BU_HALF operou 31.160 e 31.140, respectivamente. Igualar um alvo de volume não iguala exposição temporal.

## Pré-validações e critério original

O predicado do filtro MACD_BU_MINUS foi reutilizado sem redefinição, com equivalência em 6.903 oportunidades causais. Pendência histórica t−1 versus t−3 não foi reinterpretada. Sizing desligado reproduziu exatamente a base nos dois paths. Filtros públicos atuais da Spot Testnet confirmaram a viabilidade aritmética dos tamanhos de 10 USDT e UNIFORME antes das etapas pertinentes, incluindo arredondamentos. As quantidades contínuas do replay congelado foram preservadas; isso não certifica execução Testnet histórica. Nenhuma ordem foi enviada.

O gate exige melhora de pelo menos +0,025 USDT por 100 USDT contra BE_OFF_CB e net total superior a UNIFORME, nos dois paths, nas duas metades e após remover os três dias contributivos de cada comparação. O pré-registro também exige, de forma conservadora, os gates nas metades após essa exclusão. Nenhum critério foi flexibilizado; IC é diagnóstico e não veto. Apenas uma metade favorável permanece inconclusiva. Os resultados completos e as convenções temporais estão no pré-registro e relatório preservados.

## Resultado original e magnitude econômica

| BU_HALF menos comparador | LOW_FIRST | HIGH_FIRST |
| --- | ---: | ---: |
| Net por 100 USDT versus BE_OFF_CB | −0,004823 | −0,001377 |
| Net total versus BE_OFF_CB (USDT) | +9,577399 | +12,880063 |
| Net por 100 USDT versus MACD_BU_MINUS | −0,002580 | −0,000618 |
| Net total versus MACD_BU_MINUS (USDT) | −0,803981 | −0,192338 |
| Net total versus UNIFORME (USDT) | −0,387825 | +0,872125 |

Todos os oito nets absolutos foram negativos. A menor perda total contra BE_OFF_CB veio com menor notional operado, sem melhora da eficiência primária. BU_HALF ficou abaixo do bloqueio integral em net total e eficiência nos dois paths. O contraste com UNIFORME muda de sinal entre os paths. Fonte classificou **INCONCLUSIVA**, sem atingir o critério de candidato. Isso é resultado original de triagem, não validade ou conclusão científica homologada.

538 entradas BU− foram reduzidas a 10 USDT em cada path. Nos trades realmente comuns com a base, saídas e motivos permaneceram iguais e PnL foi proporcional ao notional. Efeito direto do tamanho: +12,845591/+15,148691 USDT (LOW_FIRST/HIGH_FIRST). Residual dos exclusivos: −3,268192/−2,268628 USDT, compondo os deltas totais acima. Fontes não admitidas pela base não receberam resultados contrafactuais inventados.

A primeira diferença de estado financeiro do CB aparece em 01/06 às 20:28 BRT. A primeira diferença de pausa/crise ocorre em 04/06 às 14:35 BRT. Às 15:08 de 04/06, a base bloqueia por CB e BU_HALF admite a mesma source, nos dois paths. Antes dessa mudança dependente de PnL, as admissões permaneceram iguais. São carteiras próprias: o efeito não pode ser atribuído integralmente à escolha de reduzir BU−.

## Evidências, limites e pendências

Os anexos preservam as 24 linhas econômicas por braço/path/período, deltas contra os três comparadores, gates por unidade, top3 e IC de blocos de 72h, 342 blocos HS definidos pela base, exposição média e integrada, duração, pareados e exclusivos. TRADES_TODOS.csv tem 14.016 linhas entre oito runs, com notional, MACD causal e MARÉ/ONDA. Paths e carteiras não são observações independentes. Nenhuma classificação por célula MARÉ × ONDA foi feita.

Histórico reutilizado e UNIFORME calibrado no próprio histórico não são validação independente. A revisão documental não é independente da autoria do estudo. Quantização foi verificada separadamente, não introduzida apenas nas variantes. O contador bruto legado max_positions×20 não representa exposição máxima real sob tamanho variável; a tabela econômica calcula a exposição correta e o sidecar explicita a diferença. Hipóteses, pendência documental t−1/t−3 e encerramento prévio somente do STACK_GUARD permanecem como estavam.

A primeira consolidação teve uma colisão do nome do comparador, corrigida na exportação sem rerodar políticas ou alterar resultados. Metadados do auxiliar de hashes foram completados com bytes originais preservados. As correções estão nas notas de execução; o pré-registro congelado não foi reescrito.

## Fontes preservadas

'''
 for name,label in [('PREREGISTRO_V1.md','Pré-registro original'),('RELATORIO_BU_HALF.md','Relatório integral'),('LEITURA_CIENTIFICA.md','Leitura científica original'),('TABELA_ECONOMICA.csv','Métricas completas'),('TRADES_TODOS.csv','Trades consolidados'),('NOTAS_EXECUCAO.md','Correções técnicas e preservação'),('REPRODUCAO.md','Reprodução')]:body+=f'- [{label}](versoes/MC-EXP-0007_BU_HALF_FONTES_V1/{name}).\n'
 body+='\n- [Decisão de incorporação documental](../decisoes/MC-DEC-0016_INCORPORACAO_BU_HALF.md).\n- [Pacote completo, incluindo runs/](../propostas/MC-PROP-20261027-BU-HALF-01/replay_bu_half_20261027.zip).\n'
 save(EXP,head(meta,body))
 dmeta=base_meta('MC-DEC-0016','DECISAO','Incorporação documental de BU_HALF: resultado original inconclusivo, sem melhora de eficiência contra BE_OFF_CB ou superioridade ao bloqueio integral; sem homologação ou decisão operacional.');dmeta.update(vinculos=['MC-EXP-0007','MC-LIN-0004','MC-LIN-0003'],autorizacao_expressa='atualiza mc com o resultado desse estudo')
 save(DEC,head(dmeta,'''# Incorporação documental — BU_HALF

Pedido explícito de Ernesto nesta conversa de 27/10/2026, após a entrega do estudo BU_HALF: “atualiza mc com o resultado desse estudo”. A autorização cobre a documentação dos resultados apresentados e vínculos nos registros existentes; não é aprovação científica de desempenho nem decisão de encerramento.

Incorporar MC-EXP-0007, preservar propostas, anexos e hashes; vincular à linha de exposição/sizing MC-LIN-0004 e à linha de admissão MC-LIN-0003, no histórico do seletor MACD_BU_MINUS já existente. Atualizar o índice com versões anteriores preservadas. Não criar hipótese ou cartão duplicado.

Conservar INCONCLUSIVA como classificação original do replay, com estado científico e validade homologada nulos. O pedido não aprova conclusões científicas, não encerra BU_HALF ou a linha, não reabre STACK_GUARD e não autoriza forward, novos testes ou mudanças no bot/YAML/VPS. A hipótese contextual não foi descartada por uma metade. UNIFORME permanece uma referência calibrada in-sample.

Nenhum novo replay ou recálculo econômico integra esta operação. A conferência é documental e de integridade, não independente da execução. O painel oficial não é republicado nesta incorporação; as referências científicas do índice e das linhas passam a apontar o estudo documentado.

[Experimento e evidências](../experimentos/MC-EXP-0007_BU_HALF.md).'''))
 line_note='''### Histórico documental — BU_HALF, 27/10/2026

O estudo separado de alocação [MC-EXP-0007](../experimentos/MC-EXP-0007_BU_HALF.md) comparou o bloqueio integral MACD_BU_MINUS com reduzir BU− a 10 USDT, BE_OFF_CB e UNIFORME. BU_HALF foi inferior ao bloqueio integral em net e eficiência nos dois paths; resultado original INCONCLUSIVA. O estudo não homologa o filtro, não resolve t−1/t−3 e não investiga um FAST adicional. Dados e custos próprios do replay idealizado; não transferir os deltas ao forward do braço. Sem alteração de estado do cartão, hipótese ou autorização operacional. Decisão documental: [MC-DEC-0016](../decisoes/MC-DEC-0016_INCORPORACAO_BU_HALF.md).'''
 new4='''## Alocação seletiva de capital — BU_HALF, 27/10/2026

Resultado de [MC-EXP-0007](../experimentos/MC-EXP-0007_BU_HALF.md) incorporado por [MC-DEC-0016](../decisoes/MC-DEC-0016_INCORPORACAO_BU_HALF.md). Área temática: Exposição e proteção da carteira; vínculo ao histórico do seletor MACD_BU_MINUS na MC-LIN-0003. É um experimento de sizing próprio, não duplicação de H2 ou reabertura do STACK_GUARD.

**Objetivo:** distinguir reduzir seletivamente BU− a 10 USDT de bloquear BU− ou reduzir todas as posições. **Descobertas documentadas:** classificação original INCONCLUSIVA; eficiência contra BE_OFF_CB pior nos dois paths (−0,004823/−0,001377 USDT por 100); net inferior ao bloqueio integral (−0,803981/−0,192338 USDT); contra UNIFORME, net −0,387825/+0,872125 USDT (LOW_FIRST/HIGH_FIRST). Todos os oito nets negativos. Menor perda absoluta contra BE_OFF_CB não demonstrou ganho de eficiência.

**Dúvidas e limites:** histórico reutilizado, UNIFORME calibrado na amostra, equivalência runtime não demonstrada, capital nominal sem cashgate e quantização avaliada separadamente. O efeito direto dos tamanhos e a trajetória por CB/admissões estão separados no relatório. **Situação:** incorporado documentalmente, pendente de homologação científica, sem decisão de encerramento ou implementação. **Próximo passo:** revisão do registro e deliberação humana; nenhum estudo novo ou forward está autorizado por esta incorporação.

Original, evidências e hashes em MC-EXP-0007 e seu manifesto. Nenhuma classificação por célula MARÉ × ONDA. Permanecem as decisões anteriores de STACK_GUARD e as pendências GCR/H2; este resultado não as substitui.'''
 changes=[]
 changes.append(updated(L4,'linhas/versoes/MC-LIN-0004_EXPOSICAO_E_PROTECAO_CARTEIRA_v3_antes_BU_HALF.md',new4))
 changes.append(updated(L3,'linhas/versoes/MC-LIN-0003_FILTROS_ENTRADA_E_ADMISSAO_v2_antes_BU_HALF.md','',('## Pendências',line_note)))
 changes.append(updated('README.md','versoes/README_v5_antes_BU_HALF.md','''## Alocação seletiva de capital — BU_HALF, 27/10/2026

- [MC-EXP-0007 — BU_HALF](experimentos/MC-EXP-0007_BU_HALF.md): replay idealizado de quatro políticas de sizing/seleção; classificação original INCONCLUSIVA, sem candidato e sem homologação científica.
- [MC-DEC-0016](decisoes/MC-DEC-0016_INCORPORACAO_BU_HALF.md): autorização de incorporação documental, sem decisão de encerramento ou mudança operacional.
- Linhas MC-LIN-0004 (exposição/sizing) e MC-LIN-0003 (histórico MACD_BU_MINUS), com originais, pacote e hashes preservados.

O resultado não altera hipóteses ou as decisões anteriores sobre STACK_GUARD. Não é validação independente ou autorização de forward.'''))
 originals={r['path']:r['sha256_before'] for r in changes};protected={}
 for folder in ['hipoteses','conclusoes']:
  for p in (MC/folder).rglob('*'):
   if p.is_file():protected[p.relative_to(MC).as_posix()]=sha(p)
 for rel in ['PROTOCOLO.md','PAINEL.html','PAINEL.md']:
  protected[rel]=sha(MC/rel)
 targets=[p.relative_to(W/'preparados').as_posix() for p in sorted((W/'preparados').rglob('*')) if p.is_file()]
 plan={'status_documental':'PREPARADO_PARA_INCORPORACAO_DOCUMENTAL','authorization':AUTH,'originals':originals,'protected':protected,'changes':changes,'targets':targets,'source_manifest':source_manifest,'package':pkg,'official_new_ids':['MC-EXP-0007','MC-DEC-0016','MC-ADM-0015'],'staging_proposal':PROP}
 (W/'PLANO.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8');print('PREPARED',len(targets),flush=True)

def apply():
 plan=json.loads((W/'PLANO.json').read_bytes())
 assert not (W/'APLICADO.json').exists()
 for rel,digest in {**plan['originals'],**plan['protected']}.items():assert sha(MC/rel)==digest,('CONCURRENT_CHANGE',rel)
 assert sha(ORIG/'replay_bu_half_20261027.zip')==plan['package']['sha256'],'Source ZIP changed'
 for source in plan['source_manifest']:assert sha(MC/source['origem'])==source['sha256'],('SOURCE_CHANGED',source['origem'])
 for rel in plan['targets']:
  if rel not in plan['originals']:assert not (MC/rel).exists(),('WOULD_OVERWRITE',rel)
 assert not (MC/ADM).exists();assert not (MC/PROP).exists()
 # Preserve all predecessors before replacing any current document.
 for c in plan['changes']:
  rel=c['version_preserved'];p=MC/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((W/'preparados'/rel).read_bytes());assert sha(p)==c['sha256_before']
 for rel in plan['targets']:
  if any(rel==c['version_preserved'] for c in plan['changes']):continue
  p=MC/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((W/'preparados'/rel).read_bytes())
 evidence={rel:{'sha256':sha(MC/rel),'bytes':(MC/rel).stat().st_size} for rel in plan['targets']}
 admin={'id':'MC-ADM-0015','tipo':'APOIO_DOCUMENTAL','versao':1,'status_documental':'INCORPORADO_FACTUAL','autor':'Codex','data_incorporacao':'2026-10-27','aprovacao_humana':AUTH,'resumo_executivo':None,'vinculos':['MC-EXP-0007','MC-DEC-0016','MC-LIN-0004','MC-LIN-0003'],'documentos':evidence,'anteriores_preservados':plan['changes'],'fontes_preservadas':plan['source_manifest'],'pacote_original':{'arquivo':'propostas/MC-PROP-20261027-BU-HALF-01/replay_bu_half_20261027.zip','sha256':plan['package']['sha256'],'bytes':plan['package']['bytes']},'hipoteses_conclusoes_protocolo_painel_intactos':True,'revisao_cientifica':'REVISAO_NAO_INDEPENDENTE','homologacao_cientifica':False,'nenhum_replay_executado':True}
 (MC/ADM).write_text(json.dumps(admin,ensure_ascii=False,indent=2),encoding='utf-8')
 p=MC/PROP;p.mkdir();shutil.copyfile(W/'PLANO.json',p/'PLANO_INCORPORACAO.json');shutil.copyfile(MC/ADM,p/'MANIFESTO_INCORPORACAO.json')
 # Audit proposal copies are support artifacts, not duplicate current registers.
 for name in ['PLANO_INCORPORACAO.json','MANIFESTO_INCORPORACAO.json']:
  data=json.loads((p/name).read_bytes());data['status_documental']='PROPOSTO_PARA_REVISAO';data['tipo']='APOIO_DOCUMENTAL';data['id']='MC-PROP-20261027-INCORPORACAO-BU-HALF-01-'+('PLANO' if name.startswith('PLANO') else 'MANIFESTO');data['versao']=1;data['resumo_executivo']=None;(p/name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
 for rel,digest in plan['protected'].items():assert sha(MC/rel)==digest,rel
 receipt={'applied':True,'ids':plan['official_new_ids'],'changed':list(plan['originals']),'new_manifest_sha256':sha(MC/ADM),'protected_files_verified':len(plan['protected']),'source_zip_sha256':sha(ORIG/'replay_bu_half_20261027.zip')}
 (W/'APLICADO.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(receipt),flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['prepare','apply']);a=p.parse_args();prepare() if a.stage=='prepare' else apply()
