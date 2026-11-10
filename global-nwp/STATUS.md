# Situação da coleta global

- Repositório: `slaksjsjjjjjj-dev/multimodelsss`, branch `main`.
- Workflow: `.github/workflows/SIDERAL_GLOBAL_NWP_RELEASES.yml`.
- Branch de dados publicada: `nwp-data`.
- Regiões ativadas no coletor: América do Sul e Pacífico Sul.

## Incidentes confirmados em 10 de novembro de 2026

A execução `42707513399` confirmou falta de espaço em disco durante a coleta:
- CMC-GDPS chegou a cerca de 97 MB livres e depois recebeu `OSError: [Errno 28] No space left on device`.
- GFS também recebeu `No space left on device` durante downloads NOMADS.
- Alguns artefatos de status/progresso ficaram vazios ou incompletos; o agregador parou com `JSONDecodeError` ao ler `model_status.json`.

Na execução `42796440508`, iniciada com a versão anterior do código, ICON-DWD falhou porque o objeto `dwdopen.Run` não possui `.hour`. O ciclo está em `Run.reference_time.hour`.

A configuração do workflow já usa `fail-fast: false`; portanto, o problema observado nos downloads GFS/CMC foi falta de espaço nos respectivos runners, e não cancelamento automático da matriz.

## Correções aplicadas em 10 de novembro de 2026

- **DWD ICON/AICON:** o coletor usa `Run.reference_time.hour` para escolher o horizonte correto.
- **GFS/NOMADS:** somente uma validade é baixada por vez, evitando acumular vários GRIBs regionais grandes no disco.
- **CMC-GDPS:** downloads de validades e arquivos por validade foram serializados para reduzir o pico de arquivos temporários.
- **Spool regional:** depois que os campos de uma validade são convertidos para JSON gzip e o manifesto é gravado, o NDJSON bruto dessa validade é removido. Os manifestos acumulam as validades já publicadas nos checkpoints; a finalização não precisa reconstruir dados a partir de um spool apagado.
- **Integridade dos diagnósticos:** status, resumos e manifestos do coletor são gravados por substituição atômica, evitando substituir um JSON válido por um arquivo truncado quando o disco enche.
- **Agregador:** `publish_release.py` trata status, progresso e manifestos vazios/corrompidos sem interromper toda a agregação. Diagnósticos inválidos recebem fallback, e manifestos regionais inválidos são reportados e ignorados.
- **Workflow:** as etapas de resumo e validação não quebram ao ler um progresso ausente ou incompleto; o modelo é marcado como falha quando o status não existe ou está corrompido.

## Validação

O SIDERAL CI passou no commit `a8a60cf9c245b83dbf08efe87191fcc081c425b9`:
- compilação Python dos coletores e publicadores;
- JSON dos catálogos;
- **11 testes automatizados**, incluindo DWD `Run.reference_time`, retenção de validades após a remoção do spool NDJSON e tratamento de artefatos JSON vazios/truncados.

CI: https://github.com/slaksjsjjjjjj-dev/multimodelsss/actions/runs/42800120690

**Status operacional:** as correções de código passaram no CI, mas ainda não foram confirmadas por uma coleta meteorológica completa usando esta versão. A execução `42796440508` foi iniciada antes dessas correções, com o commit `edd4a8937ff30b8e204a50d1d5649452e71a2c34`. Ela não deve ser usada como prova de que o novo coletor está funcionando. A branch `nwp-data` ainda contém a última publicação anterior, no commit `cf3cc2d6daeecebb259c26bc479c770d31f129cc`.

## Limitações conhecidas

- Alguns campos estatísticos ICON podem continuar parciais se o CDO não suportar determinado template GRIB2.
- CMC-GEPS ainda coleta os produtos estatísticos de `/products`; os GRIBs `/raw` de todos os membros seguem desativados.
- Os recortes específicos do Brasil e da Argentina continuam desativados; os dados ficam nos recortes América do Sul e Pacífico Sul.
