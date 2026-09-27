#!/usr/bin/env bash
set -euo pipefail

repository="https://github.com/Swellyhow/repair-codex-history.git"
branch="feat/session-continuity-v6"
codex_home="${CODEX_HOME:-$HOME/.codex}"
run_doctor=0

usage() {
    cat <<'EOF'
Usage: install_from_github.sh [--repo URL] [--branch NAME] [--codex-home PATH] [--doctor]

Installs the local repair-codex-history Skill from a shallow Git clone.
Only SKILL.md, agents/, scripts/, and references/ are copied.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            [[ $# -ge 2 ]] || { echo "--repo requires a URL" >&2; exit 2; }
            repository="$2"; shift 2 ;;
        --branch)
            [[ $# -ge 2 ]] || { echo "--branch requires a name" >&2; exit 2; }
            branch="$2"; shift 2 ;;
        --codex-home)
            [[ $# -ge 2 ]] || { echo "--codex-home requires a path" >&2; exit 2; }
            codex_home="$2"; shift 2 ;;
        --doctor)
            run_doctor=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        *)
            echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

command -v git >/dev/null 2>&1 || { echo 'git was not found. Install Git and retry.' >&2; exit 1; }
if (( run_doctor )); then
    command -v python3 >/dev/null 2>&1 || { echo 'python3 was not found. Install Python 3.10+ or omit --doctor.' >&2; exit 1; }
fi

temp_root="$(mktemp -d "${TMPDIR:-/tmp}/repair-codex-history.XXXXXX")"
if [[ -d "$codex_home" ]]; then
    codex_home="$(cd "$codex_home" && pwd -P)"
else
    mkdir -p "$codex_home"
    codex_home="$(cd "$codex_home" && pwd -P)"
fi
target="$codex_home/skills/repair-codex-history"
cleanup() { rm -rf "$temp_root"; }
trap cleanup EXIT

git clone --depth 1 --single-branch --branch "$branch" -- "$repository" "$temp_root"
[[ -f "$temp_root/SKILL.md" ]] || { echo 'The repository is missing SKILL.md.' >&2; exit 1; }
[[ -f "$temp_root/scripts/repair_history.py" ]] || { echo 'The repository is missing scripts/repair_history.py.' >&2; exit 1; }

mkdir -p "$target"
cp "$temp_root/SKILL.md" "$target/SKILL.md"
for directory in agents scripts references; do
    if [[ -d "$temp_root/$directory" ]]; then
        mkdir -p "$target/$directory"
        cp -R "$temp_root/$directory/." "$target/$directory/"
    fi
done

printf '{"installed":true,"repository":"%s","branch":"%s","skill_directory":"%s","doctor_run":%s}\n' \
    "$repository" "$branch" "$target" "$([[ $run_doctor -eq 1 ]] && echo true || echo false)"

if (( run_doctor )); then
    python3 "$target/scripts/repair_history.py" doctor --codex-home "$codex_home" --json
fi
