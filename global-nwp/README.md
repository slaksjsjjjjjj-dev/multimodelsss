# SIDERAL — Global NWP JSON Releases

Repositório: `slaksjsjjjjjj-dev/multimodelsss`.

## Agendamento e publicação

O workflow `.github/workflows/SIDERAL_GLOBAL_NWP_RELEASES.yml` executa duas coletas por dia, às 09Z e 21Z. Cada execução seleciona a rodada global de pelo menos nove horas antes (00Z ou 12Z), dando tempo para os provedores terminarem de publicar os arquivos. Também existe execução manual em **Actions → SIDERAL — Global NWP JSON Releases → Run workflow**. Um `push` na branch `main` executa a validação estática; ele não inicia uma coleta meteorológica automaticamente.

Cada execução cria um GitHub Release com:

- `catalog.json`: modelos, regiões, status, inventário das variáveis/níveis e relação de arquivos.
- `run-report.json`: resumo de modelos, regiões, frames e campos efetivamente gerados, incluindo modelos sem dados regionais.
- ZIPs por modelo/região, divididos em partes de até 1,5 GB. Cada ZIP contém `manifest.json` e arquivos `forecast/fNNN-partNNN.json.gz`.

Cada frame `.json.gz`, depois de descompactado, é um JSON com `metadata`, `grids` e `fields`. Cada campo mantém o nome curto e descritivo, unidade, `paramId`, tipo e valor do nível vertical, passo/intervalo, membro do ensemble quando publicado e coordenadas dos pontos. A lista de variáveis e as validades é descoberta nos GRIBs de cada rodada; não existe uma lista fixa que invente dados ausentes.

Os frames são particionados por validade e, quando o volume é grande, em partes numeradas. O `manifest.json` liga todas as partes ao respectivo F e informa as validades reais que a fonte entregou.

## Modelos configurados

| ID | Produto | Fonte | Resolução / horizonte catalogado |
|---|---|---|---|
| `ecmwf_ifs` | IFS HRES — atmosfera (`oper/fc`) e ondas (`wave/fc`) | ECMWF Open Data | 0,25°; atmosfera até F240, ondas até F360 em 00/12Z |
| `ecmwf_eps` | IFS ENS — PF (membros), EM/ES (média/dispersão), EP (probabilidades), incluindo produtos de ondas | ECMWF Open Data | 0,25°; até F360 em 00/12Z |
| `ecmwf_aifs` | AIFS Single — atmosfera e ondas | ECMWF Open Data | 0,25°; até F360 |
| `ecmwf_aifs_ens` | AIFS-ENS — CF/PF/EM/ES/EP e produtos de ondas | ECMWF Open Data | 0,25°; até F360 |
| `icon_dwd` | ICON global determinístico | DWD Open Data | Malha triangular nativa (~13 km); até F180 |
| `icon_ai` | AICON global | DWD Open Data | Malha triangular nativa (~13 km); até F180 |
| `gfs` | GFS / GEV3 — produtos `pgrb2.0p25` e `pgrb2b.0p25` | NOAA / NOMADS via Herbie | 0,25°; até F384 |
| `gfs_ai` | AIGFS — `sfc` e `pres` | NOAA / NOMADS via Herbie | 0,25°; até F384 |
| `gefs_ai` | AIGEFS — média e dispersão (`avg`/`spr`) de `sfc` e `pres` | NOAA / NOMADS via Herbie | 0,25°; até F384 |
| `hgefs` | HGEFS — média e dispersão (`avg`/`spr`) de `sfc` e `pres` | NOAA / NOMADS via Herbie | 0,25°; até F240 |
| `cmc_gdps` | GDPS determinístico — GRIB2 | ECCC Datamart | 0,15°; até F240 |
| `cmc_geps` | GEPS raw (membros individuais) e products (probabilidades/estatísticas) | ECCC Datamart | 0,5°; F384, com extensão F936 em 00Z de segunda/quinta |
| `ukmet` | UKMET Global | Met Office Weather DataHub | Bloqueado até configurar assinatura e credenciais |

**Limites conhecidos dos produtos ensemble:** AIGEFS e HGEFS estão configurados para os campos agregados média/dispersão, não para arquivar cada membro individual. EPS e AIFS-ENS coletam os tipos de produto definidos no catálogo; os membros PF são mantidos quando a fonte os publica nesses arquivos. UKMET permanece `blocked` até existir uma assinatura Global Atmospheric e uma credencial do Weather DataHub no ambiente seguro do repositório.

O coletor ECMWF não fixa `param` ou `levelist`: pede os campos que o produto disponibiliza. Para GFS/AI-GFS, Herbie seleciona os produtos integrais de superfície e pressão. O CMC GEPS usa as árvores `raw` e `products` para manter os membros e os produtos estatísticos.

## Regiões publicadas

Todas as regiões são WGS84 e definidas em `regions.json` (limites oeste/sul/leste/norte):

- América do Sul.
- Pacífico Sul — faixa principal, Ocidental e Central.
- Brasil — Norte, Nordeste, Centro-Oeste, Sudeste e Sul.
- Argentina — NOA, NEA, Cuyo, Pampa e Patagônia.

## Como consumir

1. Abra **Releases** no repositório.
2. Baixe `catalog.json` para saber quais modelos, regiões, variáveis, F e pacotes existem naquela rodada.
3. Baixe os ZIPs indicados em `regional_assets[].asset_parts[].asset`.
4. Leia o `manifest.json` e descompacte os frames `forecast/fNNN-partNNN.json.gz`.
5. Use `metadata.forecast_hour`, `valid_time`, `fields[].short_name`, `fields[].level`, `fields[].grid_type` e `grids[fields[].grid_id]` para combinar valores com coordenadas e geometria da grade (`Ni`, `Nj`, tipo de malha e modo de varredura quando disponíveis).

## Status, disponibilidade e volume

Um modelo pode terminar com status `published-ready`, `partial`, `failed` ou `blocked`. O manifesto é a fonte de verdade para os campos e horários realmente obtidos; uma validade não disponível no provedor não é fabricada. A Release é criada somente quando ao menos um pacote regional JSON foi gerado.

A coleta de todos os campos globais é volumosa, especialmente para ensembles. Os arquivos são processados um de cada vez por coletor e removidos após a conversão para limitar o espaço temporário. A matriz executa até quatro modelos em paralelo; cada modelo tem job, status e artefato próprios. Uma falha de provedor é sinalizada como falha no workflow, mas o artefato de diagnóstico é enviado antes. A etapa de publicação roda mesmo que um ou mais modelos falhem.

**Importante:** a validação estática do workflow não substitui a validação operacional de cada provedor. A validação agora exige exatamente os 13 IDs do catálogo, incluindo `cmc_geps`, e também roda em agendamentos e execuções manuais antes da coleta. A disponibilidade operacional de cada modelo só deve ser confirmada pelo `model_status.json`, pelos manifestos regionais e pelo `run-report.json` de uma execução completa.
