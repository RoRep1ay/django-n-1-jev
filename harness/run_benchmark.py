"""Score every contender against the oracle labels.

    python -m harness.run_benchmark                 # n+1-audit skill on each SKILL_MODELS + every JEV_VARIANTS
    python -m harness.run_benchmark --skip-jev      # skill only
    python -m harness.run_benchmark --skip-skill    # Jev only
    python -m harness.run_benchmark --repeat 3      # repeat every call to measure answer stability
    python -m harness.run_benchmark --label rename  # suffix the run folder name

Each run writes predictions.json and report.txt to results/runs/<UTC timestamp>[-label]/, so runs never overwrite each other.
"""
import argparse
import json
import statistics
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from harness.oracle import ROOT, label_cases
from harness.skill_check import check_skill
from harness.verdict import Verdict

THRESHOLDS = (0.3, 0.5, 0.7)
SKILL_MODELS = {'skill_opus': 'claude-opus-5-5', 'skill_sonnet': 'claude-sonnet-5-5'}
JEV_VARIANTS = {f'jev_{scope}_{kind}': (scope, kind) for kind in ('noul', 'choice') for scope in ('whole', 'fields', 'resolved')}
NAME_WIDTH = max(len(name) for name in [*SKILL_MODELS, *JEV_VARIANTS])


def get_confusion(rows: list[dict], key: str, threshold: float = 0.5) -> dict[str, float]:
    tp = sum(r[key] >= threshold and r['has_n_plus_one'] for r in rows)
    fp = sum(r[key] >= threshold and not r['has_n_plus_one'] for r in rows)
    fn = sum(r[key] < threshold and r['has_n_plus_one'] for r in rows)
    tn = len(rows) - tp - fp - fn
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {'tp': tp, 'fp': fp, 'fn': fn, 'tn': tn, 'precision': precision, 'recall': recall,
            'f1': f1, 'accuracy': (tp + tn) / len(rows)}


def get_auc(rows: list[dict], key: str) -> float:
    positives = [r[key] for r in rows if r['has_n_plus_one']]
    negatives = [r[key] for r in rows if not r['has_n_plus_one']]
    wins = sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives)
    return wins / (len(positives) * len(negatives))


def record_runs(labels: list[dict], variant: str, repeat: int, workers: int, check: Callable[[str], dict]) -> None:
    jobs = [case for _ in range(repeat) for case in labels]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda case: check(case['id']), jobs))
    for case, result in zip(jobs, results):
        case.setdefault(f'{variant}_runs', []).append(result)
    for case in labels:
        runs = case[f'{variant}_runs']
        case[variant] = statistics.mean(r['probability'] for r in runs)
        case[f'{variant}_spread'] = max(r['probability'] for r in runs) - min(r['probability'] for r in runs)


def run_skill(labels: list[dict], repeat: int, workers: int) -> None:
    for variant, model in SKILL_MODELS.items():
        def check(case_id: str) -> dict:
            result = check_skill(case_id, model)
            return {'probability': result.probability, 'verdict': result.verdict.value,
                    'fields': result.field_probabilities, 'latency_s': result.latency_seconds,
                    'input_tokens': result.input_tokens, 'usd': result.usd, 'models': result.models}

        record_runs(labels, variant, repeat, workers, check)


def run_jev(labels: list[dict], repeat: int, workers: int) -> None:
    from typesafe_sdk import TypeSafeClient

    from harness.jev_check import USD_PER_INPUT_TOKEN, check_fields, check_resolved, check_whole, load_api_key

    with TypeSafeClient(api_key=load_api_key()) as client:
        for variant, (scope, kind) in JEV_VARIANTS.items():
            jev_check = {'whole': check_whole, 'fields': check_fields, 'resolved': check_resolved}[scope]

            def check(case_id: str) -> dict:
                result = jev_check(client, case_id, kind)
                return {'probability': result.probability, 'verdict': result.verdict.value,
                        'fields': result.field_probabilities,
                        'field_verdicts': {name: v.value for name, v in result.field_verdicts.items()},
                        'verdict_probabilities': {name: {v.value: p for v, p in choices.items()}
                                                  for name, choices in result.verdict_probabilities.items()},
                        'latency_s': result.latency_seconds, 'input_tokens': result.input_tokens,
                        'usd': result.input_tokens * USD_PER_INPUT_TOKEN}

            record_runs(labels, variant, repeat, workers, check)


def build_report(labels: list[dict], contenders: list[str]) -> str:
    lines = []
    for split in ('dev', 'holdout', 'all'):
        rows = labels if split == 'all' else [c for c in labels if c['split'] == split]
        positives = sum(r['has_n_plus_one'] for r in rows)
        lines.append(f'\n=== {split}: {len(rows)} cases, {positives} with N+1 ===')
        lines.append(f"{'contender':<{NAME_WIDTH}} {'thr':>4} {'prec':>5} {'rec':>5} {'f1':>5} {'acc':>5} {'auc':>5}  tp/fp/fn/tn")
        for contender in contenders:
            auc = get_auc(rows, contender)
            for threshold in THRESHOLDS:
                m = get_confusion(rows, contender, threshold)
                lines.append(f"{contender:<{NAME_WIDTH}} {threshold:>4} {m['precision']:>5.2f} {m['recall']:>5.2f} {m['f1']:>5.2f} "
                             f"{m['accuracy']:>5.2f} {auc:>5.2f}  {m['tp']}/{m['fp']}/{m['fn']}/{m['tn']}")

    lines.append('\n=== verdicts per attempt ===')
    lines.append(f"{'contender':<{NAME_WIDTH}} {'verdict':<7} {'buggy':>6} {'clean':>6}")
    for contender in contenders:
        runs = [(r['verdict'], c['has_n_plus_one']) for c in labels for r in c[f'{contender}_runs']]
        for i, verdict in enumerate((Verdict.TRUE, Verdict.MAYBE, Verdict.FALSE)):
            buggy = sum(v == verdict.value and truth for v, truth in runs)
            clean = sum(v == verdict.value and not truth for v, truth in runs)
            lines.append(f"{contender if i == 0 else '':<{NAME_WIDTH}} {verdict.value:<7} {buggy:>6} {clean:>6}")

    lines.append('\n=== cost / latency per case ===')
    for variant in contenders:
        runs = [r for c in labels for r in c[f'{variant}_runs']]
        latencies = sorted(r['latency_s'] for r in runs)
        lines.append(f"{variant:<{NAME_WIDTH}} median {statistics.median(latencies) * 1000:.0f} ms, "
                     f"p95 {latencies[int(len(latencies) * 0.95) - 1] * 1000:.0f} ms, "
                     f"median {statistics.median(r['input_tokens'] for r in runs):.0f} input tokens, "
                     f"total ${sum(r['usd'] for r in runs):.5f} for {len(runs)} calls")
        lines.append(f"{'':<{NAME_WIDTH}} max probability spread across repeats: "
                     f"{max(c[f'{variant}_spread'] for c in labels):.3f}")

    lines.append('\n=== misclassified at 0.5 ===')
    for case in labels:
        wrong = [c for c in contenders if (case[c] >= 0.5) != case['has_n_plus_one']]
        if wrong:
            scores = ', '.join(f'{c}={case[c]:.2f}' for c in wrong)
            lines.append(f"{case['split']:<7} {case['id']:<46} truth={case['has_n_plus_one']!s:<5} {scores}")
    return '\n'.join(lines) + '\n'


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-jev', action='store_true')
    parser.add_argument('--skip-skill', action='store_true')
    parser.add_argument('--repeat', type=int, default=1)
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--label', help='suffix for the run folder name, e.g. publisher-rename')
    args = parser.parse_args()

    if args.skip_jev and args.skip_skill:
        parser.error('nothing to benchmark: --skip-jev and --skip-skill together')

    labels = label_cases()
    contenders = []
    if not args.skip_skill:
        run_skill(labels, args.repeat, args.workers)
        contenders += list(SKILL_MODELS)
    if not args.skip_jev:
        run_jev(labels, args.repeat, args.workers)
        contenders += list(JEV_VARIANTS)

    run_name = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%SZ') + (f'-{args.label}' if args.label else '')
    run_dir = ROOT / 'results' / 'runs' / run_name
    run_dir.mkdir(parents=True)
    report = build_report(labels, contenders)
    (run_dir / 'predictions.json').write_text(json.dumps(labels, indent=2, default=str) + '\n')
    (run_dir / 'report.txt').write_text(report)
    print(report)
    print(f'saved to {run_dir.relative_to(ROOT)}/')


if __name__ == '__main__':
    main()
