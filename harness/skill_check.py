"""Skill contender: a headless Claude Code agent follows the n+1-audit skill on one endpoint. The skill is
.agents/skills/n+1-audit/SKILL.md unless $N1_AUDIT_SKILL points at another SKILL.md.

The agent works in a throwaway directory holding only the models, services and the endpoint (renamed so the case id
does not leak the label). It may read files but not run code, so the audit stays static like Jev's.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from harness.verdict import SCORE, Verdict, get_most_severe

ROOT = Path(__file__).resolve().parent.parent
SKILL_PATH = Path(os.environ.get('N1_AUDIT_SKILL', ROOT / '.agents/skills/n+1-audit/SKILL.md'))
TOOLS = 'Read,Grep,Glob'
TIMEOUT_SECONDS = 600
MARK_VERDICT = {'covered': Verdict.FALSE, 'verify': Verdict.MAYBE, 'missing': Verdict.TRUE}

PROMPT = """Run the n+1-audit procedure from your system prompt on the `View` list endpoint in endpoint.py.
The models are in library/models.py and the service helpers in library/services.py.

This is a static audit: you cannot run code, so skip Step 3 (query-count test). Walk every key of the response object
per Step 2.

End your reply with exactly one fenced ```json block of the form
{"fields": {"<response key>": "covered" | "missing" | "verify"}}
where covered is ✅, missing is ❌ and verify is ⚠️."""


@dataclass
class SkillResult:
    probability: float
    verdict: Verdict
    field_probabilities: dict[str, float]
    latency_seconds: float
    input_tokens: int
    usd: float
    models: list[str]


def build_workdir(case_id: str, workdir: Path) -> None:
    (workdir / 'library').mkdir()
    for name in ('models.py', 'services.py'):
        shutil.copy(ROOT / 'library' / name, workdir / 'library' / name)
    shutil.copy(ROOT / 'cases' / f'{case_id}.py', workdir / 'endpoint.py')


def parse_marks(reply: str) -> dict[str, str]:
    blocks = re.findall(r'```json\s*(\{.*?\})\s*```', reply, re.DOTALL)
    if not blocks:
        raise ValueError(f'no ```json block in agent reply:\n{reply}')
    marks = json.loads(blocks[-1])['fields']
    unknown = {mark for mark in marks.values() if mark not in MARK_VERDICT}
    if not marks or unknown:
        raise ValueError(f'bad field marks {marks!r} in agent reply:\n{reply}')
    return marks


def check_skill(case_id: str, model: str) -> SkillResult:
    with tempfile.TemporaryDirectory(prefix='n1-skill-') as tmp:
        workdir = Path(tmp)
        build_workdir(case_id, workdir)
        command = [
            'claude', '-p', PROMPT,
            '--output-format', 'json',
            '--append-system-prompt', SKILL_PATH.read_text(),
            '--tools', TOOLS,
            '--allowedTools', TOOLS,
            '--setting-sources', 'project',
            '--strict-mcp-config',
            '--no-session-persistence',
            '--model', model,
        ]
        completed = subprocess.run(command, cwd=workdir, capture_output=True, text=True,
                                   timeout=TIMEOUT_SECONDS, check=True)
    output = json.loads(completed.stdout)
    if output['is_error']:
        raise RuntimeError(f'{case_id}: claude -p failed: {output["result"]}')
    field_verdicts = {name: MARK_VERDICT[mark] for name, mark in parse_marks(output['result']).items()}
    verdict = get_most_severe(field_verdicts.values())
    usage = output['usage']
    return SkillResult(
        probability=SCORE[verdict],
        verdict=verdict,
        field_probabilities={name: SCORE[v] for name, v in field_verdicts.items()},
        latency_seconds=output['duration_ms'] / 1000,
        input_tokens=usage['input_tokens'] + usage['cache_creation_input_tokens'] + usage['cache_read_input_tokens'],
        usd=output['total_cost_usd'],
        models=sorted(output['modelUsage']),
    )
