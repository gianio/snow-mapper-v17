#!/usr/bin/env python3
"""Build the workflows of the public RUNNER repository from this repo's own.

Why: this repository can then be private. Its heavy jobs (data cycle, site
build, tiles, app builds: ~15 000 runner minutes a month) run in a public
repository, where GitHub Actions minutes are free. That repository holds no
code -- only these generated workflows, which check this repository out
with a read-only token.

What changes per workflow (everything else is copied as is):
- every `actions/checkout` checks out CODE_REPO (repository variable) with
  the secret CODE_TOKEN, at the commit the relay sent, else main;
- `push` / `pull_request` triggers become `repository_dispatch` (sent by
  .github/workflows/relay.yml in this repository, which also applies the
  original `paths` filters and names the workflows to run);
- a pull-request run reports its result back to the commit in CODE_REPO
  as a status, so the PR shows it;
- artifacts that carry source code or signed app binaries are not uploaded
  (anyone can download a public repository's artifacts);
- this repository's PR-only checks (validate) stay here.

    python tools/runner_repo/make_runner.py --out <runner-repo-checkout>
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
# the originals: still in .github/workflows until the switch, afterwards in
# tools/runner_repo/workflows (moved there so this repository stops running them)
SRC_DIRS = [ROOT / "tools" / "runner_repo" / "workflows", ROOT / ".github" / "workflows"]


def src(name: str) -> Path:
    for d in SRC_DIRS:
        if (d / name).exists():
            return d / name
    raise SystemExit(f"workflow {name} not found")

# moved to the runner; validate.yml stays (PR check, ~2 min) with relay.yml
MOVE = json.loads((Path(__file__).with_name("moved.json")).read_text())
# artifact uploads left out in public: source, signed or installable builds
DROP_ARTIFACT = ("xcode", "ipa", "aab", "apk")

# events that now arrive as repository_dispatch keep their meaning via the
# payload's original event name
REWRITE = [
    ("github.event_name == 'pull_request'", "(github.event_name == 'pull_request' || github.event.client_payload.event == 'pull_request')"),
    ("github.event_name == 'push'", "(github.event_name == 'push' || github.event.client_payload.event == 'push')"),
    ("github.event_name != 'push'", "(github.event_name != 'push' && github.event.client_payload.event != 'push')"),
    ("default: https://gianio.github.io/snow-mapper-v17\n", "default: ''\n"),
]
# workflows that push back to the code repository (opt-in publish) keep the
# token in git; they need CODE_TOKEN with Contents: write for that
PUSHES_BACK = {"variant-a.yml"}

DISPATCH_IF = "(github.event_name != 'repository_dispatch' || contains(github.event.client_payload.wf, '{wf}'))"
REF = "${{ github.event.client_payload.sha || 'main' }}"


class _Dumper(yaml.SafeDumper):
    """Multi-line strings (the run: scripts) stay readable as | blocks."""


def _str(dumper, s):
    style = "|" if "\n" in s else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", s, style=style)


_Dumper.add_representer(str, _str)


def _checkout(step: dict, keep_creds: bool) -> dict:
    w = dict(step.get("with") or {})
    w["repository"] = "${{ vars.CODE_REPO }}"
    w["token"] = "${{ secrets.CODE_TOKEN }}"
    w.setdefault("ref", REF)
    if not keep_creds:
        w["persist-credentials"] = False
    out = {k: v for k, v in step.items() if k != "with"}
    out["with"] = w
    return out


def _drop(step: dict) -> bool:
    u = str(step.get("uses", ""))
    if not u.startswith("actions/upload-artifact"):
        return False
    name = str((step.get("with") or {}).get("name", "")).lower()
    return any(k in name for k in DROP_ARTIFACT)


STATUS_STEP = {
    "name": "Report to the code repository",
    "if": "always() && github.event_name == 'repository_dispatch' && github.event.client_payload.sha",
    "env": {"GH_TOKEN": "${{ secrets.CODE_TOKEN }}", "STATE": "${{ job.status }}",
            "SHA": "${{ github.event.client_payload.sha }}", "CTX": "runner/{wf}/${{{{ github.job }}}}",
            "URL": "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}"},
    "run": 'case "$STATE" in success) S=success;; cancelled) S=error;; *) S=failure;; esac\n'
           'gh api -X POST "repos/${{ vars.CODE_REPO }}/statuses/$SHA" -f state="$S" -f context="$CTX" '
           '-f target_url="$URL" -f description="runner: $STATE" >/dev/null',
}


def convert(name: str, d: dict) -> dict:
    on = d.pop(True, None) or d.pop("on", None) or {}
    if isinstance(on, (str, list)):
        on = {k: None for k in ([on] if isinstance(on, str) else on)}
    on = dict(on)
    from_code = [k for k in ("push", "pull_request") if k in on]
    for k in from_code:
        on.pop(k)
    if from_code:
        on["repository_dispatch"] = {"types": ["code"]}
    out = {"name": d.pop("name", name)}
    out["on"] = on
    out.update(d)
    for jid, job in (out.get("jobs") or {}).items():
        if from_code:
            cond = DISPATCH_IF.format(wf=name)
            job["if"] = f"({job['if']}) && {cond}" if job.get("if") else cond
        steps = []
        for st in job.get("steps") or []:
            if _drop(st):
                continue
            if str(st.get("uses", "")).startswith("actions/checkout"):
                st = _checkout(st, name in PUSHES_BACK)
            steps.append(st)
        if from_code and steps:
            s = dict(STATUS_STEP)
            s["env"] = {k: (v.format(wf=name.removesuffix(".yml")) if k == "CTX" else v) for k, v in STATUS_STEP["env"].items()}
            steps.append(s)
        if "steps" in job or steps:   # a job that calls a reusable workflow has none
            job["steps"] = steps
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="checkout of the runner repository")
    a = ap.parse_args()
    dst = Path(a.out) / ".github" / "workflows"
    dst.mkdir(parents=True, exist_ok=True)
    for f in MOVE:
        d = yaml.safe_load(src(f).read_text(encoding="utf-8"))
        text = yaml.dump(convert(f, d), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=200)
        for old, new in REWRITE:
            text = text.replace(old, new)
        head = (f"# GENERATED from {f} of the code repository by tools/runner_repo/make_runner.py.\n"
                "# Do not edit here: change the original and regenerate.\n")
        (dst / f).write_text(head + text, encoding="utf-8")
        print("wrote", dst / f)
    shutil.copy(Path(__file__).with_name("RUNNER_README.md"), Path(a.out) / "README.md")


if __name__ == "__main__":
    main()
