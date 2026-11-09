from __future__ import annotations

import argparse
import base64
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote


def git(
    command: list[str],
    *,
    cwd: Path,
    capture: bool = False,
    check: bool = True,
) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    output = result.stdout.strip()
    if check and result.returncode:
        raise RuntimeError(
            f"Comando falhou ({result.returncode}): {' '.join(command)}\n{output[-6000:]}"
        )
    if not check and result.returncode not in (0, 1):
        raise RuntimeError(
            f"Comando falhou ({result.returncode}): {' '.join(command)}\n{output[-6000:]}"
        )
    return output


def remote_branch_sha(
    repo_url: str,
    branch: str,
    extra_header: str,
    *,
    cwd: Path,
) -> str:
    output = git(
        [
            "git", "-c", "http.version=HTTP/1.1",
            "-c", f"http.extraheader={extra_header}",
            "ls-remote", "--heads", repo_url, f"refs/heads/{branch}",
        ],
        cwd=cwd,
    )
    return output.split()[0] if output else ""


def push_head(
    *,
    repo_url: str,
    branch: str,
    extra_header: str,
    cwd: Path,
    force: bool = False,
) -> None:
    local_sha = git(["git", "rev-parse", "HEAD"], cwd=cwd)
    delays = (2, 5, 10, 20, 30, 45)

    for attempt, delay in enumerate((*delays, None), start=1):
        # A conexão pode cair depois que o GitHub já aceitou o push.
        # Confira o SHA remoto antes de retransmitir o mesmo commit.
        remote_sha = remote_branch_sha(repo_url, branch, extra_header, cwd=cwd)
        if remote_sha == local_sha:
            print(f"Push confirmado: {branch} @ {local_sha}")
            return

        command = [
            "git",
            "-c", "http.version=HTTP/1.1",
            "-c", "http.postBuffer=524288000",
            "-c", "http.lowSpeedLimit=1000",
            "-c", "http.lowSpeedTime=120",
            "-c", f"http.extraheader={extra_header}",
            "push",
        ]
        if force:
            command.append("--force")
        command.extend([repo_url, f"HEAD:refs/heads/{branch}"])

        result = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode == 0:
            print(result.stdout.strip())
            return

        output = result.stdout.strip()
        print(
            f"Push {attempt}/7 falhou (código {result.returncode}); "
            f"verificando e tentando novamente.\n{output[-2500:]}"
        )

        remote_after = remote_branch_sha(repo_url, branch, extra_header, cwd=cwd)
        if remote_after == local_sha:
            print(f"GitHub recebeu o commit apesar da queda de conexão: {local_sha}")
            return

        if delay is not None:
            time.sleep(delay)

    raise RuntimeError(
        f"Não foi possível publicar {branch}. Commit local: {local_sha}; "
        f"commit remoto: {remote_branch_sha(repo_url, branch, extra_header, cwd=cwd)}"
    )


def commit_and_push(
    message: str,
    *,
    cwd: Path,
    repo_url: str,
    branch: str,
    extra_header: str,
    force: bool = False,
) -> None:
    staged = git(["git", "diff", "--cached", "--name-only"], cwd=cwd)
    if not staged:
        return
    git(["git", "commit", "-m", message], cwd=cwd)
    push_head(
        repo_url=repo_url,
        branch=branch,
        extra_header=extra_header,
        cwd=cwd,
        force=force,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publica uma árvore de dados em commits pequenos, com retry de rede."
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token", default=os.getenv("GH_TOKEN", ""))
    parser.add_argument("--commit-message", default="Publish Sideral data")
    parser.add_argument("--batch-mb", type=int, default=8)
    parser.add_argument("--batch-files", type=int, default=200)
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.is_dir():
        raise SystemExit(f"Diretório de publicação não encontrado: {source}")
    if not args.repository or not args.token:
        raise SystemExit("GITHUB_REPOSITORY e GH_TOKEN são necessários para publicar.")

    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files:
        raise SystemExit(f"Nenhum arquivo para publicar em {source}")

    # GitHub rejects individual Git blobs >= 100 MB. Preflight before any
    # remote branch is touched; normal NWP frames are split below this limit.
    max_file_bytes = 80 * 1024 * 1024
    oversized = [
        (path, path.stat().st_size) for path in files
        if path.stat().st_size > max_file_bytes
    ]
    if oversized:
        details = "\n".join(
            f"- {path.relative_to(source)}: {size / 1048576:.1f} MiB"
            for path, size in oversized[:30]
        )
        raise SystemExit(
            "Publicação cancelada antes do push: existem arquivos acima de "
            f"80 MiB. Divida os frames antes de publicar.\n{details}"
        )

    total_bytes = sum(path.stat().st_size for path in files)
    print(
        f"Pré-validação: {len(files)} arquivos, "
        f"{total_bytes / (1024 ** 3):.2f} GiB; "
        f"maior arquivo {max(path.stat().st_size for path in files) / 1048576:.1f} MiB."
    )

    repo_url = f"https://github.com/{args.repository}.git"
    auth = base64.b64encode(f"x-access-token:{args.token}".encode()).decode()
    extra_header = f"AUTHORIZATION: basic {auth}"
    publish_id = os.getenv("GITHUB_RUN_ID") or str(int(time.time()))
    staging_branch = f"{args.branch}-staging-{publish_id}"
    batch_limit = max(1, args.batch_mb) * 1024 * 1024
    batch_file_limit = max(1, args.batch_files)

    metadata_names = {"catalog.json", "run-report.json", ".nojekyll"}
    metadata_files = [
        path for path in files
        if len(path.relative_to(source).parts) == 1
        and path.name in metadata_names
    ]
    data_files = [
        path for path in files
        if path not in metadata_files
    ]

    # Use release-assets directly: frames and diagnostics are moved there
    # by publish_release.py. Avoid a second full copy of multi-GB model data.
    work = source
    if (work / ".git").exists():
        raise SystemExit(f"Diretório de publicação já contém .git: {work}")
work = source
    git(["git", "init", "--initial-branch=publish"], cwd=work)
    git(["git", "config", "user.name", "github-actions[bot]"], cwd=work)
    git(
        [
            "git", "config", "user.email",
            "41898282+github-actions[bot]@users.noreply.github.com",
        ],
        cwd=work,
    )

    # Primeiro commit pequeno: cria a branch e envia somente o primeiro lote.
    # Catálogo/relatório ficam para o final, quando todos os dados já existem.
    (work / ".nojekyll").touch()
    git(["git", "add", "--", ".nojekyll"], cwd=work)

    pending_bytes = 0
    pending_files = 0
    batch_number = 0
    first_push = True

    def flush_batch() -> None:
        nonlocal pending_bytes, pending_files, batch_number, first_push
        if pending_files == 0:
            return
        batch_number += 1
        commit_and_push(
            f"{args.commit_message} (data batch {batch_number})",
            cwd=work,
            repo_url=repo_url,
            branch=staging_branch,
            extra_header=extra_header,
            force=first_push,
        )
        first_push = False
        print(
            f"Lote {batch_number} publicado: {pending_files} arquivos, "
            f"{pending_bytes / 1048576:.1f} MiB"
        )
        pending_bytes = 0
        pending_files = 0

    for path in data_files:
        relative = path.relative_to(source)
        git(["git", "add", "--", relative.as_posix()], cwd=work)
        pending_bytes += path.stat().st_size
        pending_files += 1

        if pending_bytes >= batch_limit or pending_files >= batch_file_limit:
            flush_batch()

    flush_batch()

    if first_push:
        # Publicações sem frames ainda podem publicar diagnósticos e catálogo.
        commit_and_push(
            f"{args.commit_message} (initialize data tree)",
            cwd=work,
            repo_url=repo_url,
            branch=staging_branch,
            extra_header=extra_header,
            force=True,
        )
        first_push = False

    # Só disponibilize o catálogo depois que os arquivos referenciados estiverem no branch.
    for path in metadata_files:
        relative = Path(path.name)
        git(["git", "add", "--", relative.as_posix()], cwd=work)

    commit_and_push(
        f"{args.commit_message} (publish catalog)",
        cwd=work,
        repo_url=repo_url,
        branch=staging_branch,
        extra_header=extra_header,
    )

    # Confirma todos os caminhos da origem no commit que ficou no GitHub.
    expected_paths = {
        path.relative_to(source).as_posix()
        for path in files
    }
    expected_paths.add(".nojekyll")
    tree_output = git(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=work)
    committed_paths = set(tree_output.splitlines())
    missing = sorted(expected_paths - committed_paths)
    if missing:
        raise RuntimeError(
            "A publicação terminou sem incluir todos os arquivos: "
            + ", ".join(missing[:40])
        )

    local_sha = git(["git", "rev-parse", "HEAD"], cwd=work)
    staged_sha = remote_branch_sha(
        repo_url, staging_branch, extra_header, cwd=work
    )
    if staged_sha != local_sha:
        raise RuntimeError(
            f"Staging não está no commit completo. Local={local_sha}; remoto={staged_sha}"
        )

    # Promote only after every data blob and the complete catalog/report
    # are present. Consumers never see a half-published nwp-data branch.
    push_head(
        repo_url=repo_url,
        branch=args.branch,
        extra_header=extra_header,
        cwd=work,
        force=True,
    )
    published_sha = remote_branch_sha(
        repo_url, args.branch, extra_header, cwd=work
    )
    if published_sha != local_sha:
        raise RuntimeError(
            f"Promoção incompleta. Local={local_sha}; remoto={published_sha}"
        )

    # The final branch now owns the complete history; remove temporary ref.
    try:
        git([
            "git", "-c", "http.version=HTTP/1.1",
            "-c", f"http.extraheader={extra_header}",
            "push", repo_url, f":refs/heads/{staging_branch}",
        ], cwd=work)
    except Exception as exc:
        print(f"Aviso: não foi possível remover staging {staging_branch}: {exc}")

    print(
        f"Publicação confirmada: "
        f"https://github.com/{args.repository}/tree/{quote(args.branch, safe='')}"
    )
    print(f"Arquivos confirmados: {len(files)}")
    print(f"Commit publicado: {local_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
