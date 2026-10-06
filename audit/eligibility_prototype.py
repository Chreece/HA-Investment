"""Counterfactual experiment only: original production source is unchanged.

Runs identical generated inputs before and after an in-memory eligibility guard.
This is not a deployed fix and does not fix data calendars, costs or forecasting.
"""
from pathlib import Path
import hashlib
import json
import types
import scientific_safety as audit

path = audit.COMP / 'validated_model.py'
source = path.read_text(encoding='utf-8')
needle = '''    for index in whole_indices:
        item = rows[index]
        price = _sf(item.get("portfolio_price") or item.get("price"))'''
replacement = '''    for index in whole_indices:
        item = rows[index]
        # Capacity can be redistributed only among positively approved names.
        if weighted_by_key.get(_identity(item), 0.0) <= EPS:
            continue
        price = _sf(item.get("portfolio_price") or item.get("price"))'''
assert source.count(needle) == 1, 'Unexpected production source; do not patch a guess'
patched = source.replace(needle, replacement)
prototype = types.ModuleType('in_memory_eligibility_prototype')
exec(compile(patched, '<audit-only in-memory guard>', 'exec'), prototype.__dict__)
result = {'base_commit': audit.BASE, 'production_sha256': hashlib.sha256(source.encode()).hexdigest(),
          'prototype_sha256': hashlib.sha256(patched.encode()).hexdigest(),
          'production_files_modified': False, 'scope': 'Eligibility guard only; same inputs, same budgets, no parameter tuning.'}
for label, implementation in [('original', audit.v), ('guarded_prototype', prototype)]:
    audit.v = implementation
    audit.checks.clear()
    audit.targeted()
    audit.randomized_contracts()
    focused = [c for c in audit.checks if c['name'].startswith('no_resurrection_') or c['name'].startswith('no_history_cannot')]
    result[label] = {'targeted_eligibility_checks': focused,
                     'randomized_contracts': audit.report['randomized_contracts']}
assert path.read_text(encoding='utf-8') == source
result['eligibility_counterexamples_remaining'] = sum(not c['property_holds'] for c in result['guarded_prototype']['targeted_eligibility_checks'])
assert result['eligibility_counterexamples_remaining'] == 0
assert not any(result['guarded_prototype']['randomized_contracts']['violations'].values())
(audit.OUT/'eligibility_prototype.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
print('ELIGIBILITY_PROTOTYPE_BEGIN')
print(json.dumps(result, indent=2))
print('ELIGIBILITY_PROTOTYPE_END')
