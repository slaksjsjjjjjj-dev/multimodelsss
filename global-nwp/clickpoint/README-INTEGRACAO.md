# Skew-T global Click-to-Point — Sideral

Implementação do fluxo de sondagens inspirado no METBR para **GFS-AI, ICON-AI e ECMWF IFS**, exclusivamente.

## Componentes publicados

- `.github/scripts/global_nwp_skewt_clickpoint.py`: runner executável, que lê os manifests de `nwp-data`, amostra a grade mais próxima e gera perfil JSON + imagem SPC.
- `.github/workflows/GLOBAL_GFS_AI_SKEWT_CLICKPOINT.yml`
- `.github/workflows/GLOBAL_ICON_AI_SKEWT_CLICKPOINT.yml`
- `.github/workflows/GLOBAL_ECMWF_IFS_SKEWT_CLICKPOINT.yml`

A cópia do runner na branch está como launcher Python com a fonte compactada e expandida em memória; o pacote de integração anexado à conversa contém a fonte integral e editável, mais os testes.

## Contrato de dados por modelo

| Modelo | Diretório da branch `nwp-data` | Estrutura vertical |
|---|---|---|
| GFS-AI / AIGFS | `data/gfs_ai/south_america/` | Níveis `isobaricInhPa`; campos 2 m / 10 m |
| ICON-AI / AICON | `data/icon_ai/south_america/` | 13 níveis nativos `generalVertical` |
| ECMWF IFS | `data/ecmwf_ifs/south_america/` | Níveis isobáricos + superfície nativa |

Nenhum runner é criado para AIFS, ensembles ou modelos adicionais.

### Referência vertical

- **GFS-AI:** os dados atuais não fornecem pressão nem orografia de superfície. O perfil usa o menor nível isobárico utilizável como referência próxima à superfície, marcada como proxy, sem registrá-la como elevação real do terreno.
- **ICON-AI:** sem orografia publicada, a altura é integrada hidrostaticamente entre os 13 níveis e referenciada ao nível inferior do modelo. Não é altitude MSL nem AGL real.
- **ECMWF IFS:** utiliza pressão de superfície e geopotencial/orografia de superfície do próprio IFS.

Os metadados distinguem `terrain_elevation_m` (somente quando há superfície nativa) de `reference_height_m` e de `height_reference_m`. Para GFS-AI e ICON-AI, `height_agl_m` fica nulo para não sugerir altura AGL observada.

## Integração do backend

O backend existente `progames12301-hash/sideral-backend` precisa registrar as chaves `gfs_ai`, `icon_ai` e `ecmwf_ifs`, aceitar `forecast_hour` e encaminhá-la ao workflow. O patch correspondente fica no pacote local em `backend/metbr_skewt_batch.patch`.

O backend não foi alterado por esta conexão (permissão atual somente leitura). Até o patch ser aplicado e o serviço atualizado, as chamadas do site ainda não serão roteadas para estes workflows.

Endpoints METBR já usados pelo frontend:
- `POST /api/wrf/sounding/batch/request`
- `GET /api/wrf/sounding/batch/status`
- `GET /api/wrf/sounding/batch/image`

O token do backend precisa de permissão `Actions: write` em `slaksjsjjjjjj-dev/multimodelsss`.

## Páginas do site

As versões preparadas de `gfs-ai.html`, `icon-ai.html` e `ecmwf-ifs.html` estão no pacote de integração local. Elas preservam os mapas/timeline originais e reaproveitam o modal e as exportações PNG/GIF/MP4 do fluxo METBR. O repositório do frontend não está disponível na conexão GitHub atual; portanto, essas páginas não foram publicadas no site.

## Geração temporal e falhas

São montadas as validades até F048, acrescidas da validade exata selecionada quando estiver além dessa janela. Se o horário clicado não existir no manifesto ou falhar, o workflow publica `status.json` de falha em vez de servir silenciosamente outro horário.

## Verificação

Os testes locais cobrem:
- amostragem do ponto horizontal mais próximo;
- conversão de umidade em ponto de orvalho;
- perfis ECMWF IFS, GFS-AI e ICON-AI;
- distinção entre superfície nativa e referência vertical aproximada.

A publicação dos arquivos no GitHub está concluída; **a execução real dos workflows, o backend e o site ainda dependem da integração descrita acima**.
