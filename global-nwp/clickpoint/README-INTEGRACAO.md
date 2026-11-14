# Click-to-Point Skew-T para os modelos globais operacionais

Escopo solicitado: **GFS, ICON / DWD e ECMWF IFS**. Produtos de IA/AIFS e ensembles foram excluídos.

## Workflows no branch main

- `.github/workflows/GLOBAL_GFS_SKEWT_CLICKPOINT.yml`
- `.github/workflows/GLOBAL_ICON_DWD_SKEWT_CLICKPOINT.yml`
- `.github/workflows/GLOBAL_ECMWF_IFS_SKEWT_CLICKPOINT.yml`

Cada workflow recebe latitude, longitude, validade escolhida no mapa (`forecast_hour`) e o ID de 32 caracteres criado pelo backend. O workflow publica `status.json`, `manifest.json`, imagens PNG e perfis JSON no release correspondente ao ID.

## Contrato do backend

O backend já possui as rotas batch, mas o arquivo atual `backend/metbr_skewt_batch.py` ainda não registra os modelos globais. O patch `backend/metbr_skewt_batch.patch` acrescenta os três registros e encaminha `forecast_hour` ao GitHub Actions.

O dispatcher usa `METBR_SKEWT_GITHUB_TOKEN`. Esse segredo no Render precisa de permissão `Actions: write` em `slaksjsjjjjjj-dev/multimodelsss`. A ausência ou insuficiência de permissão impede o retorno HTTP 204 do GitHub.

**O patch ainda não foi aplicado** porque a conexão GitHub desta sessão tem acesso somente de leitura ao repositório `progames12301-hash/sideral-backend`.

## Estado dos dados

O catálogo `global-nwp/catalog.json` identifica os diretórios e chaves convencionais corretos:
- GFS: `data/gfs`
- ICON / DWD: `data/icon_dwd`
- ECMWF IFS: `data/ecmwf_ifs`

Na branch `nwp-data`, no momento da conferência, apenas `ecmwf_ifs` tem manifesto publicado entre esses três. `data/gfs` e `data/icon_dwd` ainda não existem nessa branch. Os diretórios `gfs_ai` e `icon_ai` não serão usados como substitutos.

Assim, mesmo depois da ativação do backend, ECMWF IFS poderá consultar seu manifesto atual; GFS e ICON/DWD só gerarão perfis quando os respectivos manifests e arquivos `FNNN.json.gz` forem publicados.

## Nenhuma execução automática confirmada

Os workflows foram versionados no branch `main`, mas ainda não há execuções `workflow_dispatch` registradas para eles. Commits acionam o CI comum, não iniciam os runners de sondagem. A execução só começa quando o backend chama a API de dispatch após receber uma solicitação do HTML.

