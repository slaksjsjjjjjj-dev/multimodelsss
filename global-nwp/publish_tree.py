from __future__ import annotations

import argparse
import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote


def run(command: list[str], *, cwd: Path | None = None, capture: bool = False) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout.strip() if capture else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Publica uma árvore de dados em commits pequenos.")
    parser.add_argument("--source", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token", default=os.getenv("GH_TOKEN", ""))
    parser.add_argument("--commit-message", default="Publish Sideral data")
    parser.add_argument("--batch-mb", type=int, default=32)
    args = parser.parse_args()

    source = Path(args.source).resolve()
    if not source.is_dir():
        raise SystemExit(f"Diretório de publicação não encontrado: {source}")
    if not args.repository or not args.token:
        raise SystemExit("GITHUB_REPOSITORY e GH_TOKEN são necessários para publicar.")

    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not files:
        raise SystemExit(f"Nenhum arquivo para publicar em {source}")

    repo_url = f"https://github.com/{args.repository}.git"
    auth = base64.b64encode(f"x-access-token:{args.token}".encode()).decode()
    extra_header = f"AUTHORIZATION: basic {auth}"
    batch_limit = max(1, args.batch_mb) * 1024 * 1024

    with tempfile.TemporaryDirectory(prefix="sideral-publish-") as temp:
        work = Path(temp)
        run(["git", "init", "--initial-branch=publish"], cwd=work)
        run(["git", "config", "user.name", "github-actions[bot]"], cwd=work)
        run(
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            cwd=work,
        )

        def commit_and_push(message: str, first: bool) -> None:
            run(["git", "commit", "-m", message], cwd=work)
            command = [
                "git", "-c", "http.version=HTTP/1.1",
                "-c", f"http.extraheader={extra_header}",
                "push",
            ]
            if first:
                command.append("--force")
            command.extend([repo_url, f"HEAD:refs/heads/{args.branch}"])
            run(command, cwd=work)

        # Publish catalog/report first so the branch is immediately discoverable.
        metadata = [
            path for path in files
            if len(path.relative_to(source).parts) == 1
            and path.name in {"catalog.json", "run-report.json", ".nojekyll"}
        ]
        if not any(path.name == ".nojekyll" for path in metadata):
            marker = work / ".nojekyll"
            marker.touch()
            metadata.append(marker)

        for path in metadata:
            relative = Path(path.name)
            destination = work / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if path.resolve() != destination.resolve():
                shutil.copy2(path, destination)
            run(["git", "add", "--", relative.as_posix()], cwd=work)

        run(["git", "commit", "-m", f"{args.commit_message} (catalog)"], cwd=work)
        command = [
            "git", "-c", "http.version=HTTP/1.1",
            "-c", f"http.extraheader={extra_header}",
            "push", "--force", repo_url, f"HEAD:refs/heads/{args.branch}",
        ]
        run(command, cwd=work)
        pushed_any = True

        metadata_set = {str(path.resolve()) for path in metadata if path.exists()}
        data_files = [path for path in files if str(path.resolve()) not in metadata_set]
        pending_bytes = 0
        batch_number = 0

        for path in data_files:
            relative = path.relative_to(source)
            destination = work / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            run(["git", "add", "--", relative.as_posix()], cwd=work)
            pending_bytes += path.stat().st_size

            if pending_bytes >= batch_limit:
                batch_number += 1
                commit_and_push(
                    f"{args.commit_message} (data batch {batch_number})",
                    first=False,
                )
                print(f"Publicado lote {batch_number}: {pending_bytes / 1048576:.1f} MiB")
                pending_bytes = 0

        if pending_bytes or not data_files:
            batch_number += 1
            commit_and_push(
                f"{args.commit_message} (data batch {batch_number})",
                first=False,
            )
            print(f"Publicado lote {batch_number}: {pending_bytes / 1048576:.1f} MiB")

        published_ref = run(
            [
                "git", "-c", "http.version=HTTP/1.1",
                "-c", f"http.extraheader={extra_header}",
                "ls-remote", "--heads", repo_url, f"refs/heads/{args.branch}",
            ],
            capture=True,
        )
        if not published_ref:
            raise SystemExit(f"A branch {args.branch} não foi confirmada no GitHub.")

    print(f"Publicação confirmada: https://github.com/{args.repository}/tree/{quote(args.branch, safe='')}")
    print(f"Arquivos enviados: {len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
