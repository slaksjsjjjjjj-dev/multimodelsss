# Situação da coleta global

- Repositório: `slaksjsjjjjjj-dev/multimodelsss`, branch `main`.
- Workflow: `.github/workflows/SIDERAL_GLOBAL_NWP_RELEASES.yml`.
- Publicação dos dados: branch `nwp-data`.
- O workflow agenda coletas às 09Z e 21Z, selecionando a rodada com pelo menos nove horas de atraso.

## Correções aplicadas em 9 de novembro de 2026

A execução `42599167158` expôs falhas que impediam o download ou a publicação completa. Foram aplicadas as seguintes correções no código:

- GFS: o catálogo usava o adaptador `nomads-filter`, mas o despachante não o reconhecia. Agora o GFS passa pelo coletor NOMADS que já estava implementado.
- AIGFS, AIGEFS e HGEFS: a lista de validades foi alinhada à cadência de seis horas dos produtos NOAA, eliminando pedidos por arquivos F que não são publicados.
- Downloads GRIB: arquivos truncados ou respostas HTML deixam de ser aceitos como GRIB; transferências inválidas são descartadas e as chamadas ECMWF têm novas tentativas.
- ECMWF EPS: os pacotes probabilísticos são solicitados em grupos menores para reduzir falhas de transferência.
- ICON/AICON: os downloads DWD são divididos em lotes de seis validades, com JSONs e progresso salvos a cada lote. Uma interrupção preserva o que já foi processado.
- Publicação: cada validade grande é dividida em vários JSON gzip independentes, com todas as partes listadas no manifesto. Arquivos acima de 80 MiB são barrados antes do push.
- Atomicidade: os lotes são enviados primeiro a uma branch temporária; `nwp-data` só recebe a nova versão depois que todos os arquivos, catálogo e relatório foram confirmados.
- Observabilidade: jobs sem dados deixam de aparecer falsamente como sucesso; modelos parciais mantêm seus artefatos e status.

## Validação

O SIDERAL CI passou no commit `d37ff3322edf55a76634878539d0b80f4d60b9b8`, incluindo compilação dos scripts e testes de regressão para horários NOAA, cadência GFS e integridade de GRIB/gzip.

**Limite desta validação:** o CI não baixa nem processa uma rodada meteorológica real. As correções precisam ser confirmadas pela próxima execução completa do workflow NWP. O erro de regrade CDO em alguns campos estatísticos GRIB2 do ICON ainda pode deixar esses modelos como `partial`; o coletor registra a ocorrência e continua processando os demais campos.

## Status anterior

A execução de 9 de novembro `42599167158` não deve ser usada como publicação completa: GFS não reconheceu o adaptador, ICON-DWD excedeu o tempo de coleta e a publicação foi rejeitada por um arquivo maior que o limite de 100 MB do GitHub. A branch de dados daquela execução ficou incompleta; a promoção atômica evita repetir esse comportamento nas próximas execuções.
